"""
HTTP API for the digital twin.

Built on `http.server` from the standard library, deliberately. FastAPI would be
more idiomatic, but a hackathon demo that requires `pip install` on a venue
network is a demo with an extra failure mode. This runs on any machine with
Python 3.10+, offline, with `python3 run.py`.

Architecture rule: this file contains no analytics. Every number it returns is
produced by the `dtwin` package. The server's only jobs are routing, caching,
running long jobs off the request thread, and making the results JSON-safe.
"""
from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from dtwin import archetypes as arch
from dtwin import calibrate as calib
from dtwin import narrate
from dtwin.analysis import full_report
from dtwin.engine import Simulation
from dtwin.model import INF, Plant
from dtwin.whatif import (Patch, apply_patches, compare, patches_from_json,
                          rank_interventions, replicate)
from server import ingest_api, live_api

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PLANTS = os.path.join(ROOT, "plants")
DATA = os.path.join(ROOT, "data")

DEFAULT_REPS = 10
FAST_REPS = 6
FRAME_INTERVAL = 30.0          # simulated seconds per playback frame


# ==========================================================================
# JSON safety
# ==========================================================================
def jsonable(obj: Any) -> Any:
    """Recursively convert engine output into something json.dumps accepts.
    Infinity and NaN are not valid JSON, and JavaScript's JSON.parse rejects
    them, so they become null and are rendered as 'unlimited' / '--' by the UI."""
    if isinstance(obj, float):
        if math.isinf(obj) or math.isnan(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    if isinstance(obj, Plant):
        return jsonable(obj.to_dict())
    if isinstance(obj, Simulation):
        return None
    return str(obj)


def digest(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(json.dumps(jsonable(p), sort_keys=True, default=str).encode())
    return h.hexdigest()[:20]


# ==========================================================================
# Session state
# ==========================================================================
class Session:
    """One plant being worked on. Holds the baseline, its provenance, cached
    results and any background job."""

    def __init__(self, plant: Plant, provenance: Dict[str, str],
                 source: str, archetype: Optional[str] = None):
        self.id = uuid.uuid4().hex[:12]
        self.plant = plant
        self.provenance = provenance
        self.source = source
        self.archetype = archetype
        self.created = time.time()
        self.cache: Dict[str, Any] = {}
        self.jobs: Dict[str, dict] = {}
        self.lock = threading.Lock()
        self.observation: Optional[dict] = None
        self.history: List[dict] = []

    def seeds(self, reps: int) -> List[int]:
        return list(range(1, reps + 1))

    def log(self, action: str, detail: str):
        self.history.append({"t": time.time(), "action": action,
                             "detail": detail})


SESSIONS: Dict[str, Session] = {}
SESSION_LOCK = threading.Lock()


# ==========================================================================
# Computation (all of it delegates into dtwin)
# ==========================================================================
def compute_baseline(sess: Session, reps: int = DEFAULT_REPS) -> dict:
    key = "baseline:" + digest(sess.plant.to_dict(), reps)
    with sess.lock:
        if key in sess.cache:
            return sess.cache[key]

    seeds = sess.seeds(reps)
    runs = replicate(sess.plant, seeds)
    ths = [r["throughput_per_h"] for r in runs]
    mean = sum(ths) / len(ths)
    if len(ths) > 1:
        var = sum((x - mean) ** 2 for x in ths) / (len(ths) - 1)
        sd = var ** 0.5
        ci = 1.96 * sd / len(ths) ** 0.5
    else:
        sd = ci = 0.0

    # the replication closest to the mean is the one we show in detail, so the
    # state breakdown on screen is typical rather than cherry-picked
    rep_seed = seeds[min(range(len(ths)), key=lambda i: abs(ths[i] - mean))]
    sim = Simulation(sess.plant, seed=rep_seed,
                     frame_interval=FRAME_INTERVAL).run()
    report = full_report(sim, th_override=mean)

    out = {
        "plant": sess.plant.to_dict(),
        "report": report,
        "replications": {"reps": reps, "mean": mean, "sd": sd, "ci95": ci,
                         "values": ths, "shown_seed": rep_seed},
        "frames": encode_frames(sim),
        "provenance": sess.provenance,
        "provenance_summary": arch.provenance_summary(sess.provenance),
        "narration": {
            "bottleneck": narrate.narrate_bottleneck(report),
        },
    }
    with sess.lock:
        sess.cache[key] = out
    return out


def encode_frames(sim: Simulation) -> dict:
    """Flatten frames into a numeric array. One frame is
    [t, good, scrap, (buffer, busy, setup, down, blocked, starved) per stage]
    which keeps the payload small enough to send over a single request."""
    stride = 3 + 6 * len(sim.p.stages)
    flat: List[float] = []
    for f in sim.frames:
        flat.append(f["t"])
        flat.append(f["good"])
        flat.append(f["scrap"])
        for st in f["st"]:
            flat.append(st["b"])
            flat.extend(st["s"])
    return {"stride": stride, "count": len(sim.frames),
            "interval": sim.frame_interval, "data": flat,
            "stages": [s.id for s in sim.p.stages],
            "order": ["buffer", "busy", "setup", "down", "blocked", "starved"]}


def compute_whatif(sess: Session, patch_dicts: List[dict],
                   reps: int = DEFAULT_REPS) -> dict:
    key = "whatif:" + digest(sess.plant.to_dict(), patch_dicts, reps)
    with sess.lock:
        if key in sess.cache:
            return sess.cache[key]

    seeds = sess.seeds(reps)
    patches = patches_from_json(patch_dicts)
    base_runs = replicate(sess.plant, seeds)
    cmp = compare(sess.plant, patches, seeds, base_runs=base_runs)

    scen_plant = cmp["scenario_plant"]
    scen_ths = [r["throughput_per_h"] for r in cmp["scenario_runs"]]
    scen_mean = sum(scen_ths) / len(scen_ths)
    scen_seed = seeds[min(range(len(scen_ths)),
                          key=lambda i: abs(scen_ths[i] - scen_mean))]
    scen_sim = Simulation(scen_plant, seed=scen_seed,
                          frame_interval=FRAME_INTERVAL).run()
    scen_report = full_report(scen_sim, th_override=scen_mean)

    out = {
        "patches": patch_dicts,
        "described": cmp["patches"],
        "deltas": cmp["deltas"],
        "stage_shift": cmp["stage_shift"],
        "bottleneck_before": cmp["bottleneck_before"],
        "bottleneck_after": cmp["bottleneck_after"],
        "bottleneck_migrated": cmp["bottleneck_migrated"],
        "reps": reps,
        "scenario_plant": scen_plant.to_dict(),
        "scenario_report": scen_report,
        "scenario_frames": encode_frames(scen_sim),
        "narration": narrate.narrate_scenario(cmp),
    }
    with sess.lock:
        sess.cache[key] = out
    return out


def compute_advisor(sess: Session, reps: int = FAST_REPS) -> dict:
    key = "advisor:" + digest(sess.plant.to_dict(), reps)
    with sess.lock:
        if key in sess.cache:
            return sess.cache[key]
    seeds = sess.seeds(reps)
    ranked = rank_interventions(sess.plant, seeds)
    out = {"ranked": ranked, "reps": reps,
           "narration": narrate.narrate_ranking(ranked)}
    with sess.lock:
        sess.cache[key] = out
    return out


def compute_calibration(sess: Session, obs_path: str,
                        reps: int = FAST_REPS) -> dict:
    obs = calib.load_observation(obs_path)
    sess.observation = obs
    seeds = sess.seeds(reps)
    before = calib.fidelity(sess.plant, obs, seeds)
    fit = calib.calibrate(sess.plant, obs, seeds)
    return {
        "observation": {"line": obs["line"],
                        "stages": obs["stages"]},
        "before": before,
        "after": fit["after"],
        "patches": fit["patches"],
        "iterations": fit["iterations"],
        "trajectory": fit["trajectory"],
        "method": fit["method"],
        "fitted_plant": fit["fitted_plant"].to_dict(),
    }


# ==========================================================================
# Background jobs
# ==========================================================================
def start_job(sess: Session, name: str, fn, *args, **kwargs) -> dict:
    """Run a slow computation off the request thread. The UI polls the job."""
    with sess.lock:
        existing = sess.jobs.get(name)
        if existing and existing["status"] in ("running", "done"):
            return {"status": existing["status"], "job": name}
        sess.jobs[name] = {"status": "running", "started": time.time(),
                           "result": None, "error": None}

    def worker():
        try:
            res = fn(*args, **kwargs)
            with sess.lock:
                sess.jobs[name] = {"status": "done", "result": res,
                                   "error": None, "started": time.time()}
        except Exception as exc:
            with sess.lock:
                sess.jobs[name] = {"status": "error", "result": None,
                                   "error": f"{type(exc).__name__}: {exc}",
                                   "trace": traceback.format_exc()}

    threading.Thread(target=worker, daemon=True, name=f"job-{name}").start()
    return {"status": "running", "job": name}


# ==========================================================================
# Routing
# ==========================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "MaestroTwin/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):
        if os.environ.get("MAESTRO_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str,
              extra: Optional[dict] = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj: Any, code: int = 200):
        body = json.dumps(jsonable(obj)).encode()
        self._send(code, body, "application/json; charset=utf-8")

    def fail(self, code: int, msg: str, detail: str = ""):
        self.json({"error": msg, "detail": detail}, code)

    MAX_BODY = 12 * 1024 * 1024

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > self.MAX_BODY:
            raise ValueError("request body is too large")
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"request body is not valid JSON: {exc}")

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    # -- dispatch ---------------------------------------------------------
    def route(self, method: str):
        try:
            u = urlparse(self.path)
            path = u.path.rstrip("/") or "/"
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if not path.startswith("/api"):
                return self.serve_static(path)
            handler = self.match(method, path)
            if handler is None:
                return self.fail(404, "No such endpoint", path)
            handler(q)
        except ValueError as exc:
            self.fail(400, str(exc))
        except KeyError as exc:
            self.fail(404, f"Not found: {exc}")
        except BrokenPipeError:
            pass
        except Exception as exc:
            self.fail(500, f"{type(exc).__name__}: {exc}",
                      traceback.format_exc())

    def match(self, method: str, path: str):
        parts = [p for p in path.split("/") if p]        # ['api', ...]
        if method == "GET" and path == "/api/catalog":
            return self.get_catalog
        if method == "POST" and path == "/api/session":
            return self.post_session
        if method == "GET" and path == "/api/llm":
            return self.get_llm
        if method == "POST" and path == "/api/ingest/discover":
            return self.post_discover
        if method == "POST" and path == "/api/ingest/suggest":
            return self.post_suggest
        if method == "POST" and path == "/api/ingest/build":
            return self.post_build
        if method == "POST" and path == "/api/ingest/describe":
            return self.post_describe
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "session":
            sid = parts[2]
            tail = "/".join(parts[3:])
            sess = SESSIONS.get(sid)
            if sess is None:
                return lambda q: self.fail(404, "Session not found. Reload the "
                                                "page to start a new one.", sid)
            table = {
                ("GET", ""): self.get_session,
                ("GET", "baseline"): self.get_baseline,
                ("GET", "advisor"): self.get_advisor,
                ("GET", "job"): self.get_job,
                ("GET", "narrate"): self.get_narrate,
                ("POST", "whatif"): self.post_whatif,
                ("POST", "apply"): self.post_apply,
                ("POST", "reset"): self.post_reset,
                ("POST", "calibrate"): self.post_calibrate,
                ("GET", "live"): self.get_live,
                ("GET", "live/stream"): self.get_live_stream,
                ("POST", "live/start"): self.post_live_start,
                ("POST", "live/stop"): self.post_live_stop,
                ("POST", "live/connector"): self.post_live_connector,
                ("POST", "live/analyze"): self.post_live_analyze,
            }
            fn = table.get((method, tail))
            if fn:
                return lambda q: fn(q, sess)
        return None

    # -- static -----------------------------------------------------------
    def serve_static(self, path: str):
        rel = "index.html" if path == "/" else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self.fail(404, "Not found", path)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)

    # -- endpoints --------------------------------------------------------
    def get_catalog(self, q):
        plants = []
        for fn in sorted(os.listdir(PLANTS)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(PLANTS, fn)) as fh:
                    d = json.load(fh)
                plants.append({
                    "file": fn, "name": d.get("name", fn),
                    "stages": len(d.get("stages", [])),
                    "archetype": d.get("archetype"),
                })
            except (json.JSONDecodeError, OSError):
                continue
        obs = [f for f in sorted(os.listdir(DATA))
               if f.endswith(".csv")] if os.path.isdir(DATA) else []
        return self.json({"plants": plants,
                          "archetypes": arch.list_archetypes(),
                          "observations": obs})

    def post_session(self, q):
        b = self.body()
        if b.get("plant"):
            plant = Plant.parse(b["plant"])
            source, key = "upload", b["plant"].get("archetype")
        elif b.get("describe"):
            matches = arch.match_archetype(b["describe"])
            key = matches[0]["key"] if matches and matches[0]["score"] > 0 \
                else "injection_moulding_assembly"
            fname = arch.ARCHETYPES[key]["plant_file"]
            base = Plant.load(os.path.join(PLANTS, fname))
            plant, prov = arch.scale_archetype(
                base,
                line_count=int(b.get("line_count", 1)),
                shift_hours=float(b.get("shift_hours", 8)),
                throughput_target=b.get("throughput_target"))
            sess = self._new_session(plant, prov, "archetype", key)
            sess.log("create", f"Archetype {key} from description")
            return self.json({"session": sess.id,
                              "plant": plant.to_dict(),
                              "matches": matches,
                              "archetype": key,
                              "provenance": prov,
                              "provenance_summary":
                                  arch.provenance_summary(prov)})
        else:
            fname = b.get("plant_file", "mdfs.json")
            fpath = os.path.normpath(os.path.join(PLANTS, fname))
            if not fpath.startswith(PLANTS) or not os.path.isfile(fpath):
                raise ValueError(f"no such line definition: {fname}")
            plant = Plant.load(fpath)
            with open(fpath) as fh:
                key = json.load(fh).get("archetype")
            source = "library"

        errs = plant.validate()
        if errs:
            raise ValueError("; ".join(errs))
        prov = arch.default_provenance(plant, arch.REFERENCE)
        sess = self._new_session(plant, prov, source, key)
        sess.log("create", f"Loaded {plant.name}")
        return self.json({"session": sess.id, "plant": plant.to_dict(),
                          "archetype": key, "provenance": prov,
                          "provenance_summary": arch.provenance_summary(prov)})

    # -- ingestion --------------------------------------------------------
    def get_llm(self, q):
        from dtwin import llm
        self.json(llm.status())

    def post_discover(self, q):
        self.json(ingest_api.discover(self.body()))

    def post_suggest(self, q):
        self.json(ingest_api.suggest(self.body()))

    def post_build(self, q):
        b = self.body()
        plant, prov, notes, proposal = ingest_api.build(b)
        sess = self._new_session(plant, prov, "import", None)
        sess.log("create", f"Imported {plant.name} from "
                           f"{proposal.get('method')} mapping")
        self.json({"session": sess.id, "plant": plant.to_dict(),
                   "provenance": prov,
                   "provenance_summary": arch.provenance_summary(prov),
                   "notes": notes,
                   "topology": proposal.get("topology"),
                   "quality": proposal.get("quality")})

    def post_describe(self, q):
        b = self.body()
        out = ingest_api.describe(b)
        if not out.get("plant"):
            return self.json(out)
        plant = Plant.parse(out["plant"])
        errs = plant.validate()
        if errs:
            out["error"] = "; ".join(errs)
            out["plant"] = None
            out["fallback"] = "archetype"
            return self.json(out)
        sess = self._new_session(plant, out["provenance"], "described", None)
        sess.log("create", "ESTIMATED model generated from a description")
        out["session"] = sess.id
        out["provenance_summary"] = arch.provenance_summary(out["provenance"])
        self.json(out)

    # -- live intake ------------------------------------------------------
    def get_live(self, q, sess):
        svc = live_api.get(sess.id)
        if svc is None:
            return self.json({"running": False})
        snap = svc.snapshot(events=int(q.get("events", 60)))
        snap["running"] = True
        self.json(snap)

    def get_live_stream(self, q, sess):
        svc = live_api.get(sess.id)
        if svc is None:
            return self.fail(409, "No live session. Start one first.")
        live_api.stream(self, svc)

    def post_live_start(self, q, sess):
        b = self.body()
        source = str(b.get("source", "replay"))
        if source not in ("replay", "mdfs", "rest", "sql", "file"):
            raise ValueError(f"unknown live source {source!r}")
        kw = {k: v for k, v in b.items()
              if k in ("url", "field_map", "records_key", "interval", "path",
                       "query", "label")}
        if source in ("sql", "file"):
            raise ValueError(f"the {source} source is configured from the "
                             f"Data Sources view, not here")
        svc = live_api.start(sess, source=source,
                             speed=float(b.get("speed", 120.0)), **kw)
        sess.log("live", f"started live intake from {source}")
        self.json({"running": True, "connection": svc.connection_badge(),
                   "connectors": svc.statuses()})

    def post_live_stop(self, q, sess):
        live_api.stop(sess.id)
        self.json({"running": False})

    def post_live_connector(self, q, sess):
        b = self.body()
        svc = live_api.get(sess.id)
        if svc is None:
            raise ValueError("no live session")
        cid, action = str(b.get("id", "")), str(b.get("action", ""))
        if action in ("add_rest", "add_sql"):
            from dtwin.live.connectors import RESTConnector, SQLConnector
            fm = b.get("field_map") if isinstance(b.get("field_map"),
                                                  dict) else None
            interval = float(b.get("interval", 5.0))
            if action == "add_rest":
                new = RESTConnector(f"rest-{len(svc.order)}",
                                    str(b.get("label") or "REST source"),
                                    str(b["url"]), field_map=fm,
                                    interval=interval,
                                    records_key=b.get("records_key"))
            else:
                new = SQLConnector(f"sql-{len(svc.order)}",
                                   str(b.get("label") or "SQL source"),
                                   str(b["path"]), str(b["query"]),
                                   field_map=fm, interval=interval)
            svc.add(new)
            sess.log("live", f"added {action[4:]} source")
            return self.json({"connectors": svc.statuses(),
                              "connection": svc.connection_badge()})
        c = svc.connectors.get(cid)
        if c is None:
            raise ValueError(f"no connector {cid!r}")
        if action == "connect":
            c.connect()
        elif action == "disconnect":
            c.disconnect()
        elif action == "retry":
            c.next_retry = 0.0
            c.connect()
        else:
            raise ValueError(f"unknown action {action!r}")
        self.json({"connectors": svc.statuses(),
                   "connection": svc.connection_badge()})

    def post_live_analyze(self, q, sess):
        """Run the existing full_report against the live twin's current state.

        Explicit user action only: no simulation is ever triggered by an event.
        """
        svc = live_api.get(sess.id)
        if svc is None:
            raise ValueError("no live session")
        from dtwin.ingest.estimate import estimate_from_live
        plant, est = estimate_from_live(svc.twin)
        errs = plant.validate()
        if errs:
            return self.json({"error": "estimated plant is not valid",
                              "detail": errs, "estimate": est}, 400)
        sim = Simulation(plant, seed=1).run()
        rep = full_report(sim)
        sess.log("live-analysis", "ran full_report on the live state")
        self.json({"report": rep, "estimate": est,
                   "plant": plant.to_dict(),
                   "note": "Parameters estimated from the live event stream; "
                           "provenance is per-stage in `estimate`."})

    def _new_session(self, plant, prov, source, key) -> Session:
        sess = Session(plant, prov, source, key)
        with SESSION_LOCK:
            SESSIONS[sess.id] = sess
            # keep memory bounded during a long demo day
            if len(SESSIONS) > 40:
                oldest = sorted(SESSIONS.values(), key=lambda s: s.created)[0]
                SESSIONS.pop(oldest.id, None)
        start_job(sess, "advisor", compute_advisor, sess)
        return sess

    def get_session(self, q, sess):
        self.json({"session": sess.id, "plant": sess.plant.to_dict(),
                   "provenance": sess.provenance,
                   "provenance_summary": arch.provenance_summary(sess.provenance),
                   "archetype": sess.archetype, "source": sess.source,
                   "history": sess.history})

    def get_baseline(self, q, sess):
        reps = max(2, min(30, int(q.get("reps", DEFAULT_REPS))))
        self.json(compute_baseline(sess, reps))

    def get_advisor(self, q, sess):
        with sess.lock:
            job = dict(sess.jobs.get("advisor") or {})
        if job.get("status") == "done":
            return self.json({"status": "done", **job["result"]})
        if job.get("status") == "error":
            return self.json({"status": "error", "error": job["error"]})
        if job.get("status") != "running":
            start_job(sess, "advisor", compute_advisor, sess)
        return self.json({"status": "running"})

    def get_job(self, q, sess):
        name = q.get("name", "advisor")
        with sess.lock:
            job = dict(sess.jobs.get(name) or {"status": "unknown"})
        job.pop("trace", None)
        self.json(job)

    def post_whatif(self, q, sess):
        b = self.body()
        patches = b.get("patches") or []
        if not patches:
            raise ValueError("no parameter changes supplied")
        reps = max(2, min(30, int(b.get("reps", DEFAULT_REPS))))
        self.json(compute_whatif(sess, patches, reps))

    def post_apply(self, q, sess):
        """Promote a scenario to the new baseline. Used by 'apply this
        intervention' and by accepting a calibration fit."""
        b = self.body()
        patches = patches_from_json(b.get("patches") or [])
        if not patches:
            raise ValueError("no parameter changes supplied")
        with sess.lock:
            sess.plant = apply_patches(sess.plant, patches)
            sess.cache.clear()
            sess.jobs.clear()
            for p in patches:
                sess.provenance[f"{p.stage}:{p.field}"] = \
                    b.get("tier", arch.ESTIMATED)
        sess.log("apply", "; ".join(p.describe() for p in patches))
        start_job(sess, "advisor", compute_advisor, sess)
        self.json({"ok": True, "plant": sess.plant.to_dict(),
                   "provenance": sess.provenance,
                   "provenance_summary":
                       arch.provenance_summary(sess.provenance),
                   "history": sess.history})

    def post_reset(self, q, sess):
        with sess.lock:
            sess.cache.clear()
            sess.jobs.clear()
        self.json({"ok": True})

    def post_calibrate(self, q, sess):
        b = self.body()
        fname = b.get("observation", "observed_shift.csv")
        fpath = os.path.normpath(os.path.join(DATA, fname))
        if not fpath.startswith(DATA) or not os.path.isfile(fpath):
            raise ValueError(f"no such observation file: {fname}")
        name = "calibrate"
        with sess.lock:
            sess.jobs.pop(name, None)
        start_job(sess, name, compute_calibration, sess, fpath)
        self.json({"status": "running", "job": name})

    def get_narrate(self, q, sess):
        mode = q.get("mode", "bottleneck")
        reps = max(2, min(30, int(q.get("reps", FAST_REPS))))
        base = compute_baseline(sess, reps)
        report = base["report"]
        if mode == "stage":
            text = narrate.narrate_stage(report, q.get("stage", "S1"))
            ctx = report
        elif mode == "executive":
            with sess.lock:
                job = sess.jobs.get("advisor") or {}
            ranked = (job.get("result") or {}).get("ranked", []) \
                if job.get("status") == "done" else []
            text = narrate.narrate_executive(report, ranked)
            ctx = {"report": report, "ranked": ranked}
        else:
            text = narrate.narrate_bottleneck(report)
            ctx = report
        out = narrate.explain(mode, ctx, draft=text)
        self.json(out)


# ==========================================================================
def serve(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd
