"""
Parameter estimation.

Turns an observed event stream (from a file or from the live twin) into the
per-stage numbers the engine needs: processing time mean and CV, MTBF, MTTR,
yield. Everything is a plain estimator over observed durations -- no fitting
loop, no model. Where there are too few samples the estimate is refused and the
value falls back to a reference/estimated one, tagged as such.

Provenance tiers match dtwin.archetypes: observed / estimated / reference /
calibrated. A value is only "observed" when it came from enough real samples.
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Tuple

from dtwin.archetypes import CALIBRATED, ESTIMATED, OBSERVED, REFERENCE
from dtwin.model import INF, Dist, Plant, Stage

MIN_SAMPLES = 8            # below this an estimate is not claimed as observed
MIN_FAILURES = 3


def _stats(xs: List[float]) -> Tuple[Optional[float], float]:
    xs = [x for x in xs if x is not None and x > 0]
    if not xs:
        return None, 0.0
    mean = statistics.fmean(xs)
    if len(xs) < 2 or mean <= 0:
        return mean, 0.0
    return mean, min(2.0, statistics.pstdev(xs) / mean)


def _tier(n: int, floor: int = MIN_SAMPLES) -> str:
    return OBSERVED if n >= floor else ESTIMATED


# ==========================================================================
# From a normalized table
# ==========================================================================
def estimate_from_table(tbl, stage_of: Dict[str, str]) -> Dict[str, dict]:
    """Estimate per-stage parameters from a normalized event log.

    `stage_of` maps machine id -> stage id (the component mapper produces it).
    """
    per: Dict[str, dict] = {}

    def slot(sid: str) -> dict:
        return per.setdefault(sid, {
            "cycle_samples": [], "repair_samples": [], "busy_between": [],
            "machines": set(), "good": 0.0, "scrap": 0.0,
            "buffer_levels": [], "buffer_caps": [],
        })

    # explicit cycle-time column
    has_cycle = tbl.has("cycle_time")
    # state-change timeline per machine
    timeline: Dict[str, List[tuple]] = {}

    for row in tbl.rows:
        if row.get("_duplicate_of") is not None:
            continue
        mach = row.get("machine") or row.get("stage")
        sid = stage_of.get(str(mach), row.get("stage") or str(mach))
        if sid is None:
            continue
        s = slot(str(sid))
        if mach:
            s["machines"].add(str(mach))
        if has_cycle and row.get("cycle_time"):
            s["cycle_samples"].append(float(row["cycle_time"]))
        if row.get("downtime"):
            s["repair_samples"].append(float(row["downtime"]))
        if row.get("quantity") is not None:
            s["good"] += float(row["quantity"])
        if row.get("scrap") is not None:
            s["scrap"] += float(row["scrap"])
        if row.get("buffer") is not None:
            s["buffer_levels"].append(float(row["buffer"]))
        ts, st = row.get("timestamp"), row.get("state")
        if ts is not None and st:
            timeline.setdefault(str(mach), []).append((float(ts), st, str(sid)))

    # derive durations from the state timeline
    for mach, seq in timeline.items():
        seq.sort(key=lambda r: r[0])
        busy_run = 0.0
        for (t0, st, sid), (t1, _st2, _s2) in zip(seq, seq[1:]):
            dur = t1 - t0
            if dur <= 0 or dur > 86400:
                continue
            s = slot(sid)
            if st == "BUSY":
                if not has_cycle:
                    s["cycle_samples"].append(dur)
                busy_run += dur
            elif st == "DOWN":
                s["repair_samples"].append(dur)
                if busy_run > 0:
                    s["busy_between"].append(busy_run)
                busy_run = 0.0

    out: Dict[str, dict] = {}
    for sid, s in per.items():
        proc_mean, proc_cv = _stats(s["cycle_samples"])
        mttr, _ = _stats(s["repair_samples"])
        mtbf, _ = _stats(s["busy_between"])
        good, scrap = s["good"], s["scrap"]
        yld = (good / (good + scrap)) if (good + scrap) > 0 else None
        out[sid] = {
            "stage": sid,
            "machines": max(1, len(s["machines"])),
            "machine_ids": sorted(s["machines"]),
            "proc_mean_s": proc_mean, "proc_cv": round(proc_cv, 3),
            "proc_samples": len(s["cycle_samples"]),
            "proc_provenance": _tier(len(s["cycle_samples"])),
            "mttr_s": mttr, "mttr_samples": len(s["repair_samples"]),
            "mttr_provenance": _tier(len(s["repair_samples"]), MIN_FAILURES),
            "mtbf_s": mtbf, "mtbf_samples": len(s["busy_between"]),
            "mtbf_provenance": _tier(len(s["busy_between"]), MIN_FAILURES),
            "yield": yld,
            "yield_provenance": (OBSERVED if yld is not None else REFERENCE),
            "buffer_observed_max": (max(s["buffer_levels"])
                                    if s["buffer_levels"] else None),
        }
    return out


def build_plant(name: str, stage_ids: List[str], estimates: Dict[str, dict],
                stage_names: Optional[Dict[str, str]] = None,
                defaults: Optional[dict] = None
                ) -> Tuple[Plant, Dict[str, str], List[dict]]:
    """Assemble a Plant from estimates, filling gaps with reference values.

    Returns (plant, provenance, notes). Provenance keys are "<stage>.<field>",
    matching what the existing UI already renders.
    """
    defaults = defaults or {}
    ref_proc = float(defaults.get("proc_mean_s", 60.0))
    ref_buffer = defaults.get("in_buffer", -1)
    prov: Dict[str, str] = {}
    notes: List[dict] = []
    stages: List[Stage] = []

    for sid in stage_ids:
        e = estimates.get(sid, {})
        proc = e.get("proc_mean_s")
        if not proc or proc <= 0:
            proc = ref_proc
            prov[f"{sid}:proc"] = REFERENCE
            notes.append({"stage": sid, "field": "proc",
                          "detail": f"no usable cycle-time samples; using the "
                                    f"reference value {ref_proc:.0f}s"})
        else:
            prov[f"{sid}:proc"] = e.get("proc_provenance", ESTIMATED)
        cv = float(e.get("proc_cv") or 0.0)
        mttr = e.get("mttr_s")
        mtbf = e.get("mtbf_s")
        if mttr and mtbf and mtbf > 0:
            prov[f"{sid}:mtbf"] = e.get("mtbf_provenance", ESTIMATED)
            prov[f"{sid}:repair"] = e.get("mttr_provenance", ESTIMATED)
        else:
            if mttr or mtbf:
                notes.append({"stage": sid, "field": "reliability",
                              "detail": "too few failure samples to estimate "
                                        "both MTBF and MTTR; failures disabled"})
            mtbf, mttr = None, 0.0
            prov[f"{sid}:mtbf"] = REFERENCE
        yld = e.get("yield")
        if yld is None or not (0 < yld <= 1):
            yld = 1.0
            prov[f"{sid}:yield"] = REFERENCE
        else:
            prov[f"{sid}:yield"] = OBSERVED
        buf = e.get("in_buffer", ref_buffer)
        stages.append(Stage(
            id=sid, name=(stage_names or {}).get(sid, sid),
            machines=int(e.get("machines", 1) or 1),
            proc=Dist("lognorm" if cv else "det", float(proc), cv),
            in_buffer=INF if (buf is None or buf < 0) else float(buf),
            mtbf=INF if not mtbf else float(mtbf),
            repair=Dist("lognorm", float(mttr or 0.0), 0.4 if mttr else 0.0),
            yield_rate=float(yld)))
        prov[f"{sid}:machines"] = (OBSERVED if e.get("machine_ids")
                                   else REFERENCE)

    plant = Plant(name=name, stages=stages,
                  horizon=float(defaults.get("horizon_s", 28800.0)),
                  warmup=float(defaults.get("warmup_s", 3600.0)))
    return plant, prov, notes


# ==========================================================================
# From the live twin
# ==========================================================================
def estimate_from_live(twin) -> Tuple[Plant, dict]:
    """Build a Plant from what the live twin has actually observed.

    Stage topology comes from the live twin (which got it from the mapper or a
    template); the numbers come from observed durations where there are enough
    of them, and from the twin's existing plant otherwise.
    """
    base = twin.plant_copy()
    est: Dict[str, dict] = {}
    for st in base.stages:
        mss = [m for (sid, _), m in twin.machines.items() if sid == st.id]
        busy = [m.durations.get("BUSY", 0.0) for m in mss]
        down = [m.durations.get("DOWN", 0.0) for m in mss]
        cyc = list(twin.cycles.get(st.id, []))
        dts = list(twin.downtimes.get(st.id, []))
        e: Dict[str, Any] = {"stage": st.id, "machines": st.machines}
        pm, pcv = _stats(cyc)
        if pm and len(cyc) >= MIN_SAMPLES:
            e.update({"proc_mean_s": pm, "proc_cv": round(pcv, 3),
                      "proc_samples": len(cyc), "proc_provenance": OBSERVED})
            st.proc = Dist("lognorm" if pcv else "det", pm, pcv)
        else:
            e.update({"proc_mean_s": st.proc.mean, "proc_cv": st.proc.cv,
                      "proc_samples": len(cyc),
                      "proc_provenance": REFERENCE,
                      "note": "too few observed cycle samples; kept the "
                              "configured processing time"})
        mt, _ = _stats(dts)
        fails = twin.counters.get("failures", 0)
        total_busy = sum(busy)
        if mt and len(dts) >= MIN_FAILURES:
            st.repair = Dist("lognorm", mt, 0.4)
            e.update({"mttr_s": mt, "mttr_samples": len(dts),
                      "mttr_provenance": OBSERVED})
            if fails >= MIN_FAILURES and total_busy > 0:
                st.mtbf = max(1.0, total_busy / max(1, fails))
                e.update({"mtbf_s": st.mtbf, "mtbf_provenance": ESTIMATED,
                          "mtbf_samples": fails})
        elif sum(down) > 0 and fails >= MIN_FAILURES and total_busy > 0:
            mttr = sum(down) / fails
            st.repair = Dist("lognorm", mttr, 0.4)
            st.mtbf = max(1.0, total_busy / fails)
            e.update({"mttr_s": mttr, "mtbf_s": st.mtbf,
                      "mttr_provenance": ESTIMATED,
                      "mtbf_provenance": ESTIMATED, "mtbf_samples": fails,
                      "note": "MTBF/MTTR derived from observed state durations"})
        else:
            e.update({"mtbf_provenance": REFERENCE,
                      "mttr_provenance": REFERENCE,
                      "note": "not enough observed failures to estimate "
                              "reliability; kept the configured values"})
        est[st.id] = e
    return base, {"per_stage": est,
                  "events_seen": twin.counters.get("events_applied", 0),
                  "tiers": "observed = enough samples; estimated = derived; "
                           "reference = kept from the configured model"}
