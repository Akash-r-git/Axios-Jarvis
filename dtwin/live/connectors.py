"""
Connectors: everything that turns an outside data source into canonical Events.

Nothing in dtwin/engine.py, analysis.py or whatif.py may import this module.
The dependency runs one way only: connectors -> events -> LiveTwin -> Plant ->
engine. tests/test_live.py enforces that with an import-boundary test.

Connection state machine
------------------------
    disconnected --connect()--> connecting --ok--> connected
         ^                          |                 |
         |                          +--fail--> error <+
         +------- disconnect() / retry ---------------+

A connector in `error` schedules a retry with exponential backoff. It never
clears the twin's last known state; a dead source means stale data, not empty
data, and the UI says which.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from dtwin.live.events import (BUFFER, COUNTER, CYCLE, DOWNTIME, OBSERVED,
                               REPLAY, STATE_CHANGE, Event, MDFS_STATE_MAP,
                               map_vendor_state)

DISCONNECTED = "disconnected"
CONNECTING = "connecting"
CONNECTED = "connected"
ERROR = "error"

BACKOFF_BASE = 1.0
BACKOFF_MAX = 30.0


class Connector:
    """Base connector. Subclasses implement _open, _close and _read."""

    kind = "base"
    #: what this connector is honestly delivering: "live" | "replay" | "poll"
    delivery = "live"

    def __init__(self, cid: str, label: str, source_label: Optional[str] = None):
        self.id = cid
        self.label = label
        self.source_label = source_label or label
        self.state = DISCONNECTED
        self.error: Optional[str] = None
        self.last_update: Optional[float] = None
        self.attempts = 0
        self.next_retry: Optional[float] = None
        self.backoff = BACKOFF_BASE
        self.events_emitted = 0
        self.notes: List[str] = []
        self._lock = threading.Lock()

    # ---- lifecycle --------------------------------------------------------
    def connect(self) -> bool:
        with self._lock:
            self.state = CONNECTING
            self.error = None
        try:
            self._open()
        except Exception as exc:
            return self._fail(f"{type(exc).__name__}: {exc}")
        with self._lock:
            self.state = CONNECTED
            self.next_retry = None
        return True

    def disconnect(self) -> None:
        try:
            self._close()
        except Exception:
            pass
        with self._lock:
            self.state = DISCONNECTED
            self.next_retry = None
            self.error = None

    def _fail(self, msg: str) -> bool:
        with self._lock:
            self.state = ERROR
            self.error = msg
            self.attempts += 1
            self.backoff = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** min(
                self.attempts - 1, 6)))
            self.next_retry = time.time() + self.backoff
        return False

    # ---- polling ----------------------------------------------------------
    def poll(self, now: Optional[float] = None) -> List[Event]:
        """Return whatever events are due. Never raises."""
        now = time.time() if now is None else now
        if self.state == ERROR:
            if self.next_retry is None or now < self.next_retry:
                return []
            if not self.connect():          # still down: backoff has grown
                return []
        if self.state != CONNECTED:
            return []
        try:
            evs = list(self._read(now))
        except Exception as exc:
            # a source that reconnects and then fails again is still failing,
            # so the backoff keeps growing until a read actually succeeds
            self._fail(f"{type(exc).__name__}: {exc}")
            return []
        if self.attempts:
            with self._lock:
                self.attempts = 0
                self.backoff = BACKOFF_BASE
        if evs:
            self.last_update = now
            self.events_emitted += len(evs)
        return evs

    # ---- status -----------------------------------------------------------
    def status(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "label": self.label,
            "source_label": self.source_label,
            "delivery": self.delivery,
            "state": self.state, "error": self.error,
            "last_update": self.last_update,
            "attempts": self.attempts,
            "next_retry_in_s": (None if self.next_retry is None
                                else max(0.0, self.next_retry - time.time())),
            "events_emitted": self.events_emitted,
            "notes": list(self.notes),
        }

    # ---- to implement -----------------------------------------------------
    def _open(self) -> None:
        pass

    def _close(self) -> None:
        pass

    def _read(self, now: float) -> Iterable[Event]:
        return ()


# ==========================================================================
# Replay
# ==========================================================================
class ReplayConnector(Connector):
    """Replays a recorded or generated session through the normal interface.

    Everything it emits carries provenance=replay and the UI labels it REPLAY.
    It is never described as live.
    """

    kind = "replay"
    delivery = "replay"

    def __init__(self, cid: str, label: str, events: List[dict],
                 speed: float = 60.0, loop: bool = False,
                 source_label: Optional[str] = None):
        super().__init__(cid, label, source_label)
        # events: dicts with "offset" (seconds from session start) + event fields
        self.events = sorted(events, key=lambda e: e.get("offset", 0.0))
        self.speed = max(0.01, float(speed))
        self.loop = loop
        self.cursor = 0
        self.t0: Optional[float] = None
        self.completed = False

    def _open(self) -> None:
        self.t0 = time.time()
        self.cursor = 0
        self.completed = False

    def _read(self, now: float) -> Iterable[Event]:
        if self.t0 is None:
            return ()
        elapsed = (now - self.t0) * self.speed
        out: List[Event] = []
        while self.cursor < len(self.events):
            rec = self.events[self.cursor]
            if rec.get("offset", 0.0) > elapsed:
                break
            d = dict(rec)
            d.pop("offset", None)
            d["ts"] = self.t0 + rec.get("offset", 0.0) / self.speed
            d.setdefault("source", self.id)
            d["provenance"] = REPLAY
            out.append(Event.parse(d, source=self.id, provenance=REPLAY))
            self.cursor += 1
        if self.cursor >= len(self.events):
            if self.loop:
                self.t0 = now
                self.cursor = 0
            else:
                self.completed = True
        return out

    def status(self) -> dict:
        s = super().status()
        s.update({"progress": (self.cursor / len(self.events))
                  if self.events else 1.0,
                  "completed": self.completed, "speed": self.speed,
                  "total_events": len(self.events)})
        return s


class FileConnector(ReplayConnector):
    """Replays an event file uploaded by the user (JSON list or JSONL).

    Records must already be canonical events (the normalizer produces them).
    Size is capped: a file big enough to exhaust memory is refused.
    """

    kind = "file"
    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self, cid: str, label: str, path: str, speed: float = 60.0):
        recs = self._load(path)
        super().__init__(cid, label, recs, speed=speed,
                         source_label=os.path.basename(path))

    @classmethod
    def _load(cls, path: str) -> List[dict]:
        if os.path.getsize(path) > cls.MAX_BYTES:
            raise ValueError(f"event file exceeds {cls.MAX_BYTES} bytes")
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        text_s = text.strip()
        if text_s.startswith("["):
            data = json.loads(text_s)
        else:
            data = [json.loads(ln) for ln in text_s.splitlines() if ln.strip()]
        if not isinstance(data, list):
            raise ValueError("event file must contain a list of events")
        base = None
        out = []
        for d in data:
            if not isinstance(d, dict):
                continue
            ts = float(d.get("ts", d.get("timestamp", 0)) or 0)
            base = ts if base is None else base
            rec = dict(d)
            rec["offset"] = max(0.0, ts - base) if base else 0.0
            out.append(rec)
        return out


# ==========================================================================
# REST polling
# ==========================================================================
class RESTConnector(Connector):
    """Polls an HTTP endpoint returning JSON records and maps them to events.

    `field_map` says which keys hold stage / machine / state / timestamp. No
    field names are guessed: an unmapped payload produces a clear error rather
    than silent nonsense.
    """

    kind = "rest"
    delivery = "poll"
    MAX_BYTES = 2 * 1024 * 1024

    def __init__(self, cid: str, label: str, url: str,
                 field_map: Optional[Dict[str, str]] = None,
                 interval: float = 5.0, records_key: Optional[str] = None,
                 state_table: Optional[Dict[str, str]] = None,
                 opener: Optional[Callable[[str], bytes]] = None):
        super().__init__(cid, label, source_label=url)
        self.url = url
        self.interval = max(0.5, float(interval))
        self.field_map = field_map or {"stage": "stage", "machine": "machine",
                                       "state": "state", "ts": "timestamp"}
        self.records_key = records_key
        self.state_table = state_table
        self.opener = opener              # injectable for tests
        self._last_poll = 0.0
        self._seen: set = set()

    def _open(self) -> None:
        if not (self.url.startswith("http://") or
                self.url.startswith("https://")):
            raise ValueError("REST url must be http(s)")

    def _fetch(self) -> Any:
        if self.opener is not None:
            raw = self.opener(self.url)
        else:
            import urllib.request
            req = urllib.request.Request(self.url,
                                         headers={"accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read(self.MAX_BYTES + 1)
        if len(raw) > self.MAX_BYTES:
            raise ValueError("REST response too large")
        return json.loads(raw.decode("utf-8", "replace"))

    def _read(self, now: float) -> Iterable[Event]:
        if now - self._last_poll < self.interval:
            return ()
        self._last_poll = now
        data = self._fetch()
        if self.records_key:
            data = data.get(self.records_key, []) if isinstance(data, dict) \
                else []
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ValueError("REST payload is not a list of records")
        out = []
        fm = self.field_map
        for rec in data[:5000]:
            if not isinstance(rec, dict):
                continue
            key = json.dumps(rec, sort_keys=True, default=str)[:400]
            if key in self._seen:
                continue
            if len(self._seen) > 20000:
                self._seen.clear()
            self._seen.add(key)
            state, warn = map_vendor_state(rec.get(fm.get("state", "state")),
                                           self.state_table)
            if warn:
                self.notes = (self.notes + [warn])[-5:]
            ts = rec.get(fm.get("ts", "timestamp"))
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                ts = now
            out.append(Event(
                ts=ts,
                stage=str(rec.get(fm.get("stage", "stage"), "")),
                machine=str(rec.get(fm.get("machine", "machine"), "")
                            or rec.get(fm.get("stage", "stage"), "")),
                event_type=STATE_CHANGE,
                new_state=state,
                source=self.id, provenance=OBSERVED, raw=rec))
        return out


# ==========================================================================
# SQL (stdlib sqlite3, read-only, single SELECT)
# ==========================================================================
_FORBIDDEN_SQL = (";", "--", "/*", "*/", "attach", "pragma", "insert",
                  "update", "delete", "drop", "alter", "create", "replace",
                  "vacuum", "begin", "commit")


def validate_select(query: str) -> str:
    """Accept exactly one read-only SELECT. Anything else is refused."""
    q = (query or "").strip().rstrip(";").strip()
    low = q.lower()
    if not low.startswith("select"):
        raise ValueError("only a single SELECT statement is allowed")
    for bad in _FORBIDDEN_SQL:
        if bad in low:
            raise ValueError(f"disallowed token in query: {bad!r}")
    return q


class SQLConnector(Connector):
    """Polls a read-only sqlite3 database with one configured SELECT.

    The interface is driver-shaped so another driver could be added later, but
    only sqlite3 is implemented, because only sqlite3 can be tested here with
    no dependencies. No unimplemented driver is exposed in the UI.
    """

    kind = "sql"
    delivery = "poll"
    driver = "sqlite3"

    def __init__(self, cid: str, label: str, path: str, query: str,
                 field_map: Optional[Dict[str, str]] = None,
                 interval: float = 5.0,
                 state_table: Optional[Dict[str, str]] = None):
        super().__init__(cid, label, source_label=f"sqlite3:{os.path.basename(path)}")
        self.path = path
        self.query = validate_select(query)
        self.field_map = field_map or {"stage": "stage", "machine": "machine",
                                       "state": "state", "ts": "ts"}
        self.interval = max(0.5, float(interval))
        self.state_table = state_table
        self.conn = None
        self._last_poll = 0.0
        self._max_ts = 0.0

    def _open(self) -> None:
        import sqlite3
        if not os.path.isfile(self.path):
            raise FileNotFoundError(self.path)
        uri = "file:" + self.path.replace("?", "%3f") + "?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def _close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _read(self, now: float) -> Iterable[Event]:
        if now - self._last_poll < self.interval:
            return ()
        self._last_poll = now
        cur = self.conn.execute(self.query)
        fm = self.field_map
        out = []
        for row in cur.fetchmany(5000):
            rec = dict(row)
            try:
                ts = float(rec.get(fm.get("ts", "ts")))
            except (TypeError, ValueError):
                continue
            if ts <= self._max_ts:
                continue
            state, warn = map_vendor_state(rec.get(fm.get("state", "state")),
                                           self.state_table)
            if warn:
                self.notes = (self.notes + [warn])[-5:]
            out.append(Event(ts=ts,
                             stage=str(rec.get(fm.get("stage", "stage"), "")),
                             machine=str(rec.get(fm.get("machine", "machine"),
                                                 "") or ""),
                             event_type=STATE_CHANGE, new_state=state,
                             source=self.id, provenance=OBSERVED, raw=rec))
        if out:
            self._max_ts = max(e.ts for e in out)
        return out


# ==========================================================================
# MDFS
# ==========================================================================
class MDFSProfile:
    """Topic/field profile for the MaestroHub Digital Factory Simulator.

    The real topics, REST paths and field names live in docs/mdfs_interface.md.
    That file was NOT supplied with this repository, so no profile is shipped:
    inventing topic names would be exactly the fake-integration failure mode
    this project refuses. A profile can be injected (from the doc, or by a
    test) and the connector then works generically against it.
    """

    def __init__(self, event_topic: str, fields: Dict[str, str],
                 rest_base: Optional[str] = None,
                 rest_paths: Optional[Dict[str, str]] = None,
                 state_table: Optional[Dict[str, str]] = None,
                 origin: str = "injected"):
        self.event_topic = event_topic
        self.fields = fields
        self.rest_base = rest_base
        self.rest_paths = rest_paths or {}
        self.state_table = state_table or dict(MDFS_STATE_MAP)
        self.origin = origin


class MQTTTransport:
    """paho-mqtt transport, lazily imported and entirely optional.

    A fake client can be injected, which is how the message-handling path is
    tested with no broker. The real socket path is unverified until it is run
    against an actual MDFS instance — see the README.
    """

    def __init__(self, host: str, port: int = 1883, topic: str = "#",
                 username: Optional[str] = None, password: Optional[str] = None,
                 client_factory: Optional[Callable[[], Any]] = None):
        self.host, self.port, self.topic = host, port, topic
        self.username, self.password = username, password
        self.client_factory = client_factory
        self.client = None
        self.messages: List[tuple] = []
        self.connected = False
        self.available_error: Optional[str] = None
        self._lock = threading.Lock()

    @staticmethod
    def library_available() -> bool:
        try:
            import paho.mqtt.client            # noqa: F401
            return True
        except Exception:
            return False

    def _make_client(self):
        if self.client_factory is not None:
            return self.client_factory()
        try:
            import paho.mqtt.client as mqtt
        except Exception as exc:
            self.available_error = ("paho-mqtt is not installed; MQTT "
                                    "transport unavailable")
            raise RuntimeError(self.available_error) from exc
        return mqtt.Client()

    def on_message(self, _client, _userdata, msg):
        payload = getattr(msg, "payload", b"")
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8", "replace")
        with self._lock:
            if len(self.messages) < 20000:
                self.messages.append((getattr(msg, "topic", ""), payload))

    def connect(self) -> None:
        c = self._make_client()
        if self.username:
            try:
                c.username_pw_set(self.username, self.password or "")
            except Exception:
                pass
        c.on_message = self.on_message
        c.connect(self.host, self.port, 30)
        c.subscribe(self.topic)
        try:
            c.loop_start()
        except Exception:
            pass
        self.client = c
        self.connected = True

    def disconnect(self) -> None:
        if self.client is not None:
            for fn in ("loop_stop", "disconnect"):
                try:
                    getattr(self.client, fn)()
                except Exception:
                    pass
        self.client = None
        self.connected = False

    def drain(self) -> List[tuple]:
        with self._lock:
            out, self.messages = self.messages, []
        return out


class MDFSConnector(Connector):
    """MQTT-first connector for the MaestroHub Digital Factory Simulator.

    MDFS is a 4-station serial line (OP10-CNC, OP20-CNC, OP30-WASH, OP40-TEST)
    with 10-part buffers. MQTT events drive the live twin; REST polling covers
    buffers, downtime records and the equipment master.

    BLOCKED and STARVED arriving from MDFS are OBSERVED states, recorded as
    such. IDLE is carried through as IDLE, not folded into STARVED.

    Without docs/mdfs_interface.md and a captured session (neither of which is
    present in this repository) there is no honest profile to ship, so the
    connector reports itself UNCONFIGURED and the UI says so. Supplying a
    profile plus MDFS_MQTT_HOST/MDFS_MQTT_PORT/MDFS_REST_BASE turns it on.
    """

    kind = "mdfs"
    delivery = "live"

    def __init__(self, cid: str = "mdfs", label: str = "MDFS (MaestroHub DFS)",
                 profile: Optional[MDFSProfile] = None,
                 transport: Optional[MQTTTransport] = None,
                 rest_opener: Optional[Callable[[str], bytes]] = None):
        super().__init__(cid, label)
        self.profile = profile
        self.transport = transport
        self.rest_opener = rest_opener
        self.host = os.environ.get("MDFS_MQTT_HOST")
        self.port = int(os.environ.get("MDFS_MQTT_PORT", "1883") or 1883)
        self.rest_base = os.environ.get("MDFS_REST_BASE")
        self.unmapped: Dict[str, int] = {}
        self.rest_interval = float(os.environ.get("MDFS_REST_INTERVAL", "10"))
        self._last_rest = 0.0
        if not self.configured():
            self.notes.append(self.unconfigured_reason())

    # ---- configuration ----------------------------------------------------
    def configured(self) -> bool:
        return self.profile is not None and (
            self.transport is not None or bool(self.host))

    def unconfigured_reason(self) -> str:
        if self.profile is None:
            return ("UNCONFIGURED: docs/mdfs_interface.md and a captured MDFS "
                    "session were not supplied, so no topic/field profile "
                    "exists. Topics and field names are not guessed. Showing "
                    "the engine-generated replay instead.")
        return ("UNCONFIGURED: set MDFS_MQTT_HOST (and optionally "
                "MDFS_MQTT_PORT, MDFS_REST_BASE) to connect.")

    # ---- lifecycle --------------------------------------------------------
    def _open(self) -> None:
        if not self.configured():
            raise RuntimeError(self.unconfigured_reason())
        if self.transport is None:
            if not MQTTTransport.library_available():
                raise RuntimeError("paho-mqtt is not installed; install it or "
                                   "use REST polling")
            self.transport = MQTTTransport(
                self.host, self.port, self.profile.event_topic,
                os.environ.get("MDFS_MQTT_USER"),
                os.environ.get("MDFS_MQTT_PASSWORD"))
        self.transport.connect()

    def _close(self) -> None:
        if self.transport is not None:
            self.transport.disconnect()

    # ---- message handling -------------------------------------------------
    def parse_message(self, topic: str, payload: str) -> Optional[Event]:
        """Turn one MDFS MQTT message into a canonical event.

        Purely a function of the injected profile: no topic or field name is
        hard-coded here.
        """
        try:
            rec = json.loads(payload)
        except Exception:
            return None
        if not isinstance(rec, dict):
            return None
        f = self.profile.fields
        ts = rec.get(f.get("ts", "ts"))
        try:
            ts = float(ts)
            if ts > 1e11:                    # epoch milliseconds
                ts /= 1000.0
        except (TypeError, ValueError):
            ts = time.time()
        stage = str(rec.get(f.get("stage", "stage"), "") or "")
        machine = str(rec.get(f.get("machine", "machine"), "") or stage)
        etype = STATE_CHANGE
        value = None
        new_state = prev_state = None
        if f.get("state") and f["state"] in rec:
            new_state, warn = map_vendor_state(rec[f["state"]],
                                               self.profile.state_table)
            if warn:
                key = str(rec[f["state"]])
                self.unmapped[key] = self.unmapped.get(key, 0) + 1
                self.notes = (self.notes + [warn])[-5:]
            if f.get("previous_state") and f["previous_state"] in rec:
                prev_state, _ = map_vendor_state(rec[f["previous_state"]],
                                                 self.profile.state_table)
        elif f.get("buffer") and f["buffer"] in rec:
            etype, value = BUFFER, rec[f["buffer"]]
        elif f.get("cycle") and f["cycle"] in rec:
            etype, value = CYCLE, rec[f["cycle"]]
        elif f.get("downtime") and f["downtime"] in rec:
            etype, value = DOWNTIME, rec[f["downtime"]]
        elif f.get("count") and f["count"] in rec:
            etype, value = COUNTER, rec[f["count"]]
        try:
            value = float(value) if value is not None else None
        except (TypeError, ValueError):
            value = None
        return Event(ts=ts, stage=stage, machine=machine, event_type=etype,
                     previous_state=prev_state, new_state=new_state,
                     source=self.id, value=value, provenance=OBSERVED, raw=rec)

    def _read(self, now: float) -> Iterable[Event]:
        out: List[Event] = []
        for topic, payload in self.transport.drain():
            ev = self.parse_message(topic, payload)
            if ev is not None:
                out.append(ev)
        if self.rest_base and now - self._last_rest >= self.rest_interval:
            self._last_rest = now
            out.extend(self._poll_rest())
        return out

    def _poll_rest(self) -> List[Event]:
        """REST polling for buffers / downtime / equipment master.

        Paths come from the profile (i.e. from docs/mdfs_interface.md). With no
        profile paths configured this is a no-op rather than a guess.
        """
        out: List[Event] = []
        for what, path in (self.profile.rest_paths or {}).items():
            url = self.rest_base.rstrip("/") + "/" + path.lstrip("/")
            try:
                if self.rest_opener is not None:
                    raw = self.rest_opener(url)
                else:
                    import urllib.request
                    with urllib.request.urlopen(url, timeout=10) as resp:
                        raw = resp.read(2 * 1024 * 1024)
                data = json.loads(raw.decode("utf-8", "replace"))
            except Exception as exc:
                self.notes = (self.notes + [f"REST {what}: {exc}"])[-5:]
                continue
            recs = data if isinstance(data, list) else data.get("items", [])
            f = self.profile.fields
            for rec in recs if isinstance(recs, list) else []:
                if not isinstance(rec, dict):
                    continue
                key = {"buffers": "buffer", "downtime": "downtime"}.get(what)
                if key is None or not f.get(key) or f[key] not in rec:
                    continue
                try:
                    val = float(rec[f[key]])
                except (TypeError, ValueError):
                    continue
                out.append(Event(
                    ts=float(rec.get(f.get("ts", "ts"), time.time()) or
                             time.time()),
                    stage=str(rec.get(f.get("stage", "stage"), "")),
                    machine=str(rec.get(f.get("machine", "machine"), "")
                                or rec.get(f.get("stage", "stage"), "")),
                    event_type=BUFFER if key == "buffer" else DOWNTIME,
                    value=val, source=self.id, provenance=OBSERVED, raw=rec))
        return out

    def status(self) -> dict:
        s = super().status()
        s.update({
            "configured": self.configured(),
            "unconfigured_reason": (None if self.configured()
                                    else self.unconfigured_reason()),
            "mqtt_library": MQTTTransport.library_available(),
            "mqtt_note": (None if MQTTTransport.library_available() else
                          "unavailable - install paho-mqtt (REST polling "
                          "still works)"),
            "unmapped_states": dict(self.unmapped),
            "profile_origin": (self.profile.origin if self.profile else None),
        })
        return s
