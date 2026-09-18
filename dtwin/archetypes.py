"""
Industry reference archetypes and parameter provenance.

The honest position on data, which every member of the team should be able to
state out loud:

    Public sources give us industry, process type and scale. They do not give us
    cycle times, MTBF, buffer capacities or scrap rates -- that data lives in an
    MES behind a firewall and is competitively sensitive. So we infer the line
    ARCHETYPE from public information, populate it from published OEE and
    process benchmarks, and mark every parameter we did not measure. The moment
    a customer uploads one shift of real data, calibration replaces estimates
    with measured values and reports how far off we were.

Every parameter therefore carries a provenance tier:

    measured   the customer's own data (upload, MES export, DB endpoint)
    reference  our curated archetype, sourced from published benchmarks
    estimated  inferred for this specific company by scaling an archetype

The UI renders the tier as a coloured dot next to each field. A twin built
entirely from `estimated` values is still useful for relative comparisons --
"which stage is the constraint" is far more robust than "what is the absolute
throughput" -- and the UI says so rather than pretending otherwise.
"""
from __future__ import annotations

import copy
from typing import Dict, List

from .model import Plant

MEASURED, REFERENCE, ESTIMATED = "measured", "reference", "estimated"
# Added with the intake layer: values read from a real event stream are
# "observed"; values fitted by dtwin.calibrate are "calibrated". MEASURED is
# kept as the original name of the observed tier so existing sessions and tests
# are unaffected.
OBSERVED, CALIBRATED = "observed", "calibrated"

TIER_NOTE = {
    MEASURED: "From your uploaded shift data.",
    OBSERVED: "Read directly from the connected data source.",
    CALIBRATED: "Fitted to your observed shift by the calibrator.",
    REFERENCE: "Industry benchmark for this process type.",
    ESTIMATED: "Inferred for this company by scaling the archetype.",
}

# --------------------------------------------------------------------------
# Archetype library
# --------------------------------------------------------------------------
# Each entry is a complete plant definition plus sourcing metadata. Cycle times
# and availability figures are order-of-magnitude representative of the process
# class; they are reference values, not measurements of any specific plant, and
# the `source_note` says so.

ARCHETYPES: Dict[str, dict] = {
    "injection_moulding_assembly": {
        "label": "Injection moulding and assembly",
        "industry": "Consumer durables, automotive interior, appliance housings",
        "typical_oee": 0.65,
        "source_note": (
            "Cycle times reflect typical thermoplastic housing moulding (30-60 s) "
            "with downstream CNC finishing. Availability reflects published "
            "discrete-manufacturing OEE bands of 60-70%."
        ),
        "signals": ["injection", "moulding", "molding", "plastic", "housing",
                    "appliance", "consumer durable", "enclosure"],
        "plant_file": "mdfs.json",
    },
    "smt_electronics": {
        "label": "SMT electronics assembly",
        "industry": "PCB assembly, consumer electronics, industrial controls",
        "typical_oee": 0.72,
        "source_note": (
            "Placement-rate-driven line. Reflow is a fixed-takt tunnel, so it "
            "behaves as parallel capacity rather than a variable-time station. "
            "AOI yield reflects typical first-pass rates for fine-pitch assembly."
        ),
        "signals": ["pcb", "smt", "electronics", "circuit", "assembly",
                    "semiconductor", "placement", "reflow"],
        "plant_file": "smt_line.json",
    },
    "pharma_packaging": {
        "label": "Pharmaceutical packaging",
        "industry": "Pharma, nutraceutical, medical device packaging",
        "typical_oee": 0.58,
        "source_note": (
            "Changeover-dominated: validated cleaning and line-clearance between "
            "batches drives the constraint more than cycle time. Inspection yield "
            "reflects regulated reject criteria."
        ),
        "signals": ["pharma", "pharmaceutical", "medicine", "drug", "tablet",
                    "blister", "vial", "nutraceutical", "medical device"],
        "plant_file": "pharma_packaging.json",
    },
}


def list_archetypes() -> List[dict]:
    return [{"key": k, **{kk: vv for kk, vv in v.items() if kk != "plant_file"}}
            for k, v in ARCHETYPES.items()]


def match_archetype(description: str) -> List[dict]:
    """Rank archetypes by keyword overlap with a company/industry description.

    This is deliberately a transparent keyword matcher, not a model. If you
    later put an LLM in front of it, the LLM's job is to produce the description
    string and pick from THIS list -- it never invents parameters. Keeping the
    final selection auditable is the whole point.
    """
    text = (description or "").lower()
    scored = []
    for key, a in ARCHETYPES.items():
        hits = [s for s in a["signals"] if s in text]
        scored.append({
            "key": key,
            "label": a["label"],
            "industry": a["industry"],
            "typical_oee": a["typical_oee"],
            "source_note": a["source_note"],
            "score": len(hits),
            "matched_terms": hits,
        })
    scored.sort(key=lambda r: -r["score"])
    return scored


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------
PROVENANCE_FIELDS = ["proc.mean", "proc.cv", "machines", "in_buffer", "mtbf",
                     "repair.mean", "yield_rate", "setup_time", "batch_size"]


def default_provenance(plant: Plant, tier: str = REFERENCE) -> Dict[str, str]:
    """Tag every field of every stage with one tier. Key format `S3:proc.mean`."""
    return {f"{s.id}:{f}": tier for s in plant.stages for f in PROVENANCE_FIELDS}


def scale_archetype(plant: Plant, line_count: int = 1,
                    shift_hours: float = 8.0,
                    throughput_target: float | None = None) -> tuple:
    """Adapt a reference archetype to a specific company using only the kind of
    information that genuinely is public: how many lines they run, what shift
    pattern, and a stated annual or daily output.

    We scale PARALLEL CAPACITY (machine counts), never the physics. A moulding
    machine's cycle time does not change because a company is larger; the number
    of machines does. That distinction is what keeps this defensible.
    """
    p = copy.deepcopy(plant)
    prov = default_provenance(p, REFERENCE)
    p.horizon = shift_hours * 3600.0
    p.warmup = min(p.warmup, p.horizon * 0.15)

    if throughput_target and throughput_target > 0:
        current = min(s.capacity_per_h() * p.downstream_yield(i)
                      for i, s in enumerate(p.stages))
        factor = throughput_target / max(current, 1e-9)
        if factor > 1.05:
            # add parallel machines until every stage clears the target
            for i, s in enumerate(p.stages):
                need = s.machines
                while (s.capacity_per_h() * p.downstream_yield(i) < throughput_target
                       and need < s.machines * 12):
                    need += 1
                    s.machines = need
                prov[f"{s.id}:machines"] = ESTIMATED

    if line_count > 1:
        for s in p.stages:
            s.machines *= line_count
            prov[f"{s.id}:machines"] = ESTIMATED

    return p, prov


def provenance_summary(prov: Dict[str, str]) -> dict:
    counts = {MEASURED: 0, REFERENCE: 0, ESTIMATED: 0, OBSERVED: 0,
              CALIBRATED: 0}
    for v in prov.values():
        if v in counts:
            counts[v] += 1
    # observed and calibrated values are as trustworthy as measured ones
    counts[MEASURED] += counts.pop(OBSERVED) + counts.pop(CALIBRATED)
    total = max(1, sum(counts.values()))
    confidence = (counts[MEASURED] * 1.0 + counts[REFERENCE] * 0.5) / total
    if counts[MEASURED] == total:
        verdict = "Fully calibrated against your data."
    elif counts[MEASURED] > 0:
        verdict = ("Partly calibrated. Absolute figures carry the uncertainty of "
                   "the uncalibrated stages; relative comparisons between stages "
                   "remain reliable.")
    else:
        verdict = ("Not yet calibrated. Treat absolute throughput as indicative. "
                   "Bottleneck ranking and what-if direction are still meaningful "
                   "because they depend on relative stage capacity, not absolute "
                   "cycle times.")
    return {"counts": counts, "total": total, "confidence": round(confidence, 3),
            "verdict": verdict}
