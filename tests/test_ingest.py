"""
Ingestion tests: normalizer, component mapper, estimator, anomaly detection,
and the universality test that takes a non-MDFS file all the way through the
unchanged engine.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.harness import Suite, assert_true, approx                # noqa: E402
from dtwin.analysis import full_report                              # noqa: E402
from dtwin.engine import Simulation                                 # noqa: E402
from dtwin.model import Plant                                       # noqa: E402
from dtwin.whatif import Patch, compare, replicate                  # noqa: E402
from dtwin.ingest import (apply_edits, normalize, normalize_state,  # noqa: E402
                          parse_timestamp, propose, to_plant,
                          validate_proposal)

S = Suite("ingestion tests")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "greenfield_line.csv")


def fixture_bytes() -> bytes:
    with open(FIXTURE, "rb") as fh:
        return fh.read()


DIRTY = b"""asset_id;process;event_time;status;processing_time (min);qty;rejects
MC_01;Forming;2026-03-02T06:00:00Z;running;1.5;10;0
MC_01;Forming;2026-03-02T06:02:00Z;RUNNING;1.5;10;0
MC_01;Forming;not-a-date;running;1.5;10;0
MC_02;Welding;1772000000;levitating;2.0;8;1
MC_02;Welding;1772000120000;idle;-4.0;8;1
MC_02;Welding;2026-03-02T06:10:00Z;blocked;2.0;8;1
MC_01;Forming;2026-03-02T06:00:00Z;running;1.5;10;0
"""


# ==========================================================================
@S.check("Aliases, units, timestamps and state words are normalized")
def t_normalizer():
    tbl = normalize(fixture_bytes(), "greenfield_line.csv")
    m = tbl.mapping()
    assert_true(m.get("Work Centre") == "machine",
                f"machine alias failed: {m}")
    assert_true(m.get("Process Step") == "stage", "stage alias failed")
    assert_true(m.get("Event Ts") == "timestamp", "timestamp alias failed")
    assert_true(m.get("Op Status") == "state", "state alias failed")
    assert_true(m.get("Cycle (ms)") == "cycle_time", "cycle alias failed")
    assert_true(m.get("Reject Qty") == "scrap", "scrap alias failed")
    assert_true(m.get("Queue Len") == "buffer", "buffer alias failed")
    assert_true(tbl.kind == "event_log", f"classified as {tbl.kind}")

    row = next(r for r in tbl.rows if r.get("cycle_time"))
    assert_true(1.0 < row["cycle_time"] < 600.0,
                f"milliseconds were not converted to seconds: {row['cycle_time']}")
    assert_true(row["state"] == "BUSY", f"RUN -> {row['state']}")
    assert_true(row["timestamp"] > 1_700_000_000, "ISO timestamp not parsed")

    # unit and timestamp conversions in isolation
    assert_true(abs(parse_timestamp(1772000000000)[0] - 1772000000.0) < 1e-6,
                "epoch milliseconds not detected")
    assert_true(parse_timestamp("2026-03-02T06:00:00Z")[0] > 0, "ISO failed")
    assert_true(parse_timestamp("garbage")[0] is None, "garbage accepted")
    assert_true(normalize_state("Changeover")[0] == "SETUP", "changeover")
    assert_true(normalize_state("no_material")[0] == "STARVED", "starved word")
    assert_true(normalize_state("levitating")[0] is None, "unknown state mapped")

    dirty = normalize(DIRTY, "dirty.csv")
    dm = dirty.mapping()
    assert_true(dm.get("asset_id") == "machine" and dm.get("process") == "stage",
                f"semicolon CSV with different names failed: {dm}")
    minutes = next(r["cycle_time"] for r in dirty.rows
                   if r.get("cycle_time") and r["cycle_time"] > 0)
    approx(minutes, 90.0, 1e-6, "minutes -> seconds")


@S.check("Every data-quality warning fires on dirty data")
def t_quality():
    q = normalize(DIRTY, "dirty.csv").quality
    kinds = {w["kind"] for w in q["warnings"]}
    for expected in ("bad_timestamps", "unknown_states", "impossible_values",
                     "duplicates"):
        assert_true(expected in kinds, f"{expected} not reported: {kinds}")
    assert_true(q["bad_timestamps"] == 1, f"bad timestamps {q['bad_timestamps']}")
    assert_true(q["unknown_states"] == 1, f"unknown states {q['unknown_states']}")
    assert_true(q["impossible_values"] == 1,
                f"impossible values {q['impossible_values']}")
    assert_true(q["duplicates"] == 1, f"duplicates {q['duplicates']}")
    assert_true("levitating" in q["unknown_state_values"],
                "the offending state value was not reported")

    thin = normalize(b"machine,widgets\nMC_01,5\n", "thin.csv")
    kinds = {w["kind"] for w in thin.quality["warnings"]}
    assert_true("missing_field" in kinds, "missing fields not reported")
    assert_true("missing_relationship" in kinds,
                "missing machine->stage relationship not reported")
    assert_true("unmapped_columns" in kinds, "unmapped columns not reported")


@S.check("The mapper groups machines into ordered stages with reasons")
def t_mapper():
    tbl = normalize(fixture_bytes(), "greenfield_line.csv")
    prop = propose(tbl, "Greenfield")
    assert_true(prop["method"] == "stage_column",
                f"expected the explicit stage column, got {prop['method']}")
    ids = [s["id"] for s in prop["stages"]]
    assert_true(ids == ["Raw Material", "Machining", "Inspection", "Assembly",
                        "Packaging"], f"stage order wrong: {ids}")
    mach = next(s for s in prop["stages"] if s["id"] == "Machining")
    assert_true(mach["machine_count"] == 2,
                f"parallel machines not detected: {mach['machine_count']}")
    for s in prop["stages"]:
        assert_true(0 < s["confidence"] <= 1, "confidence out of range")
        assert_true(s["reason"], "no reason given for a stage")
        assert_true(s["provenance"]["proc"] in ("observed", "estimated",
                                                "reference"),
                    "bad provenance tier")
    assert_true(prop["topology"]["serial"], "clean serial data flagged as not")

    # naming-pattern fallback
    csv2 = (b"machine,timestamp,status,cycle_time_s\n" +
            b"".join(f"mc_{i:02d},{1772000000 + i},running,{10 + i}\n".encode()
                     for i in range(1, 5)) * 3)
    p2 = propose(normalize(csv2, "naming.csv"), "Naming")
    assert_true(p2["method"] == "naming_pattern",
                f"naming fallback not used: {p2['method']}")
    assert_true([s["id"] for s in p2["stages"]] ==
                ["MC_01", "MC_02", "MC_03", "MC_04"],
                f"naming order wrong: {[s['id'] for s in p2['stages']]}")

    # last resort: first appearance
    csv3 = b"machine,timestamp,status\nlathe,1772000000,running\nmill,1772000060,running\n"
    p3 = propose(normalize(csv3, "fa.csv"), "FA")
    assert_true(p3["method"] == "first_appearance", p3["method"])
    assert_true(p3["stages"][0]["confidence"] < 0.6,
                "a guess must not be presented with high confidence")


@S.check("Mapping edits are applied and validated deterministically")
def t_edits():
    tbl = normalize(fixture_bytes(), "greenfield_line.csv")
    prop = propose(tbl, "Greenfield")
    prop = apply_edits(prop, [
        {"id": "Machining", "name": "CNC Machining", "machine_count": 3},
        {"id": "Packaging", "include": False},
    ])
    mach = next(s for s in prop["stages"] if s["id"] == "Machining")
    assert_true(mach["machine_count"] == 3 and mach["name"] == "CNC Machining",
                "edit not applied")
    assert_true("machine_count" in mach["edited"], "edit not recorded")
    plant, prov, notes = to_plant(prop)
    assert_true([s.id for s in plant.stages] ==
                ["Raw Material", "Machining", "Inspection", "Assembly"],
                "excluded stage still present")
    assert_true(plant.stages[1].machines == 3, "edited machine count lost")

    bad = json.loads(json.dumps(prop))
    bad["stages"][0]["proc_mean_s"] = -5
    errs = validate_proposal(bad)
    assert_true(errs, "validator accepted a negative processing time")
    bad2 = json.loads(json.dumps(prop))
    bad2["stages"][1]["id"] = bad2["stages"][0]["id"]
    assert_true(validate_proposal(bad2), "validator accepted duplicate ids")


@S.check("Data implying branching or merging is flagged, not flattened")
def t_non_serial():
    rows = ["machine,stage,next_stage,timestamp,status,cycle_time_s"]
    for i in range(6):
        rows.append(f"m1,CUT,PAINT,{1772000000 + i},running,20")
        rows.append(f"m2,CUT,POLISH,{1772000100 + i},running,20")
        rows.append(f"m3,PAINT,PACK,{1772000200 + i},running,25")
        rows.append(f"m4,POLISH,PACK,{1772000300 + i},running,25")
    prop = propose(normalize("\n".join(rows).encode(), "branch.csv"), "Branchy")
    topo = prop["topology"]
    assert_true(not topo["serial"], "branching data was not flagged")
    kinds = {f["kind"] for f in topo["flags"]}
    assert_true("branching" in kinds, f"no branching flag: {kinds}")
    assert_true("merging" in kinds, f"no merging flag: {kinds}")
    assert_true("cannot represent" in topo["verdict"],
                "the verdict does not admit the limitation")
    plant, prov, notes = to_plant(prop)
    assert_true(any(n["field"] == "topology" for n in notes),
                "the built plant carries no topology caveat")


@S.check("The estimator reports sample counts and refuses thin evidence")
def t_estimator():
    tbl = normalize(fixture_bytes(), "greenfield_line.csv")
    prop = propose(tbl, "Greenfield")
    mach = next(s for s in prop["stages"] if s["id"] == "Machining")
    assert_true(mach["samples"]["cycle"] > 100,
                f"too few cycle samples counted: {mach['samples']}")
    approx(mach["proc_mean_s"], 62.0, 8.0, "machining cycle time")
    assert_true(mach["provenance"]["proc"] == "observed",
                "plenty of samples but not marked observed")
    assert_true(mach["mttr_s"] and 300 < mach["mttr_s"] < 550,
                f"MTTR not recovered from the state log: {mach['mttr_s']}")
    assert_true(mach["provenance"]["mttr"] == "estimated",
                "a single failure sample must not be called observed")
    rm = next(s for s in prop["stages"] if s["id"] == "Raw Material")
    assert_true(rm["mttr_s"] is None, "invented a repair time with no failures")
    plant, prov, notes = to_plant(prop)
    assert_true(prov["Raw Material:mtbf"] == "reference",
                "unevidenced reliability was not tagged reference")
    assert_true(prov["Machining:proc"] == "observed", "observed tier lost")


@S.check("A clean event stream produces zero anomalies")
def t_anomaly_clean():
    from dtwin.live.anomaly import AnomalyDetector
    from dtwin.live.events import BUSY, Event, STATE_CHANGE
    from dtwin.live.twin import LiveTwin
    plant = Plant.load(os.path.join(ROOT, "plants", "mdfs.json"))
    twin = LiveTwin(plant)
    det = AnomalyDetector(plant)
    t = 1_772_000_000.0
    found = []
    for i in range(300):
        sid = plant.stages[i % len(plant.stages)].id
        mach = [m for (s, m) in twin.machines if s == sid][0]
        e = Event(ts=t + i * 30, stage=sid, machine=mach,
                  event_type=STATE_CHANGE, new_state=BUSY, source="t")
        twin.apply(e)
        found += det.observe(e, twin)
    assert_true(found == [], f"clean stream produced anomalies: {found[:2]}")


@S.check("Abnormal downtime, repeat failures and cycle drift are detected")
def t_anomaly_detect():
    from dtwin.live.anomaly import AnomalyDetector
    from dtwin.live.events import BUSY, CYCLE, DOWN, DOWNTIME, Event
    from dtwin.live.twin import LiveTwin
    plant = Plant.load(os.path.join(ROOT, "plants", "mdfs.json"))
    sid = plant.stages[0].id
    twin = LiveTwin(plant)
    mach = [m for (s, m) in twin.machines if s == sid][0]
    det = AnomalyDetector(plant)
    t = 1_772_000_000.0
    found = []

    long_stop = max(60.0, plant.stages[0].repair.mean * 6 + 600)
    e = Event(ts=t, stage=sid, machine=mach, event_type=DOWNTIME,
              value=long_stop, source="t")
    twin.apply(e)
    found += det.observe(e, twin)
    assert_true(any(a["kind"] == "downtime_duration" for a in found),
                f"long stop not flagged: {found}")

    for i in range(6):
        e1 = Event(ts=t + 100 + i * 60, stage=sid, machine=mach,
                   new_state=DOWN, source="t")
        twin.apply(e1)
        found += det.observe(e1, twin)
        e2 = Event(ts=t + 130 + i * 60, stage=sid, machine=mach,
                   new_state=BUSY, previous_state=DOWN, source="t")
        twin.apply(e2)
        found += det.observe(e2, twin)
    assert_true(any(a["kind"] == "repeat_failures" for a in found),
                "repeated failures in a window not flagged")

    cyc = plant.stages[0].proc.mean
    for i in range(12):
        e = Event(ts=t + 1000 + i, stage=sid, machine=mach,
                  event_type=CYCLE, value=cyc * 3.0, source="t")
        twin.apply(e)
        found += det.observe(e, twin)
    assert_true(any(a["kind"] == "cycle_deviation" for a in found),
                "cycle-time drift not flagged")
    for a in found:
        for key in ("kind", "stage", "ts", "observed", "expected", "severity",
                    "message"):
            assert_true(key in a, f"anomaly missing {key}: {a}")


@S.check("UNIVERSALITY: a non-MDFS CSV runs the unchanged engine end to end")
def t_universality():
    tbl = normalize(fixture_bytes(), "greenfield_line.csv")
    assert_true(tbl.kind == "event_log", "fixture not read as an event log")
    prop = propose(tbl, "Greenfield Components")
    errs = validate_proposal(prop)
    assert_true(not errs, f"proposal invalid: {errs}")
    plant, prov, notes = to_plant(prop)
    assert_true(len(plant.stages) == 5, f"{len(plant.stages)} stages")
    assert_true(not plant.validate(), f"plant invalid: {plant.validate()}")

    report = full_report(Simulation(plant, seed=1).run())
    for key in ("kpis", "bottlenecks", "propagation", "losses",
                "inefficiencies"):
        assert_true(key in report, f"full_report missing {key}")
    assert_true(report["kpis"]["throughput_per_h"] > 0, "no throughput")
    bn = report["bottlenecks"][0]["id"]
    assert_true(bn in [s.id for s in plant.stages], "bottleneck is not a stage")

    seeds = [1, 2, 3, 4]
    base = replicate(plant, seeds)
    assert_true(len(base) == len(seeds), "replication count wrong")
    scen = compare(plant, [Patch(bn, "machines", "add", 1)], seeds)
    assert_true("throughput_per_h" in scen["deltas"],
                f"what-if returned no throughput delta: {list(scen['deltas'])}")
    d = scen["deltas"]["throughput_per_h"]
    for key in ("base", "scenario", "delta", "ci_low", "ci_high",
                "significant"):
        assert_true(key in d, f"delta missing {key}")
    assert_true(scen["bottleneck_before"] and "bottleneck_after" in scen,
                "what-if did not report bottleneck migration")
    assert_true(plant.stages[[s.id for s in plant.stages].index(bn)].machines
                == next(s["machine_count"] for s in prop["stages"]
                        if s["id"] == bn),
                "the what-if mutated the imported baseline plant")


@S.check("Ingestion never leaks into the engine (architecture boundary)")
def t_boundary():
    for fn in ("engine.py", "analysis.py", "whatif.py"):
        src = open(os.path.join(ROOT, "dtwin", fn)).read().lower()
        for token in ("ingest", "normalize", "connector", "gemini", "mdfs",
                      "csv"):
            assert_true(token not in src,
                        f"dtwin/{fn} mentions {token!r}; the engine must stay "
                        f"source-agnostic")


@S.check("Gemini output is validated and can be rejected")
def t_llm_guard():
    from dtwin import llm
    accepted, rejected = [], []

    def fake_generate(system, prompt, **kw):
        return ('```json\n[{"field": "Work Centre", "target": "machine", '
                '"confidence": 0.9, "reason": "asset identifier"},'
                '{"field": "Nonexistent", "target": "machine"},'
                '{"field": "Queue Len", "target": "telepathy"}]\n```')

    real = llm.generate
    llm.generate = fake_generate
    try:
        accepted, rejected = llm.suggest_mapping(
            ["Work Centre", "Queue Len"], ["machine", "stage", "buffer"])
    finally:
        llm.generate = real
    assert_true(len(accepted) == 1 and accepted[0]["field"] == "Work Centre",
                f"validator accepted junk: {accepted}")
    assert_true(len(rejected) == 2, f"rejections not reported: {rejected}")
    assert_true(all("gemini" in a["source"] for a in accepted),
                "suggestion not labelled as coming from the model")

    def bad_factory(system, prompt, **kw):
        return json.dumps({"name": "X", "stages": [
            {"id": "A", "proc": {"mean": -3}, "yield": 4.0, "machines": 0},
            {"id": "B", "proc": {"mean": 30}, "mtbf": 600, "yield": 0.99}]})

    llm.generate = bad_factory
    try:
        plant_d, prov, notes = llm.describe_factory("two stage line")
    finally:
        llm.generate = real
    p = Plant.parse(plant_d)
    assert_true(not p.validate(), f"invalid plant survived: {p.validate()}")
    assert_true(all(v == "estimated" for v in prov.values()),
                "generated values were not tagged ESTIMATED")
    assert_true(notes, "silently repaired bad values without saying so")
    assert_true(llm.status()["configured"] in (True, False)
                and "GEMINI_API_KEY" not in json.dumps(llm.status()).replace(
                    "No GEMINI_API_KEY in the environment.", ""),
                "the key leaked into the status payload")


if __name__ == "__main__":
    S.export_pytest(globals())
    p, f = S.run()
    sys.exit(0 if f == 0 else 1)
else:
    S.export_pytest(globals())
