"""
Component mapper.

Decides which machines belong to which stage and in what order, then builds a
canonical Plant. Three strategies, tried in order, each with a confidence and a
human-readable reason:

  1. an explicit stage/process/operation column          (confidence 0.95)
  2. machine naming patterns, e.g. mc_01 / OP20-CNC-2    (confidence 0.7)
  3. first appearance in the data                        (confidence 0.4)

Topology: the engine is documented as serial-only (see ENGINE_SPEC.md), so this
mapper emits serial multi-stage lines with parallel machines per stage. Data
that implies branching or merging is FLAGGED, not flattened — the user is told
the twin cannot represent it rather than being handed a quietly wrong model.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from dtwin.ingest.estimate import build_plant, estimate_from_table
from dtwin.model import Plant

_NAME_RE = re.compile(r"^(?P<base>.*?)(?P<major>\d+)(?:[^0-9]*?(?P<minor>\d+))?"
                      r"[^0-9]*$")


def _sort_key(v: Any) -> Tuple:
    s = str(v)
    m = re.search(r"(\d+)", s)
    return (0, int(m.group(1)), s) if m else (1, 0, s)


# ==========================================================================
def _by_stage_column(tbl) -> Optional[dict]:
    if not tbl.has("stage"):
        return None
    order: List[str] = []
    members: Dict[str, List[str]] = {}
    for row in tbl.rows:
        sid = row.get("stage")
        if not sid:
            continue
        sid = str(sid)
        if sid not in members:
            members[sid] = []
            order.append(sid)
        mach = str(row.get("machine") or sid)
        if mach not in members[sid]:
            members[sid].append(mach)
    if not order:
        return None
    # if the stage names carry numbers, trust them over file order
    if all(re.search(r"\d", s) for s in order):
        order = sorted(order, key=_sort_key)
        reason_order = "ordered by the numbering in the stage column"
    else:
        reason_order = "ordered by first appearance in the file"
    return {"method": "stage_column", "order": order, "members": members,
            "confidence": 0.95,
            "reason": f"an explicit stage column groups the machines; "
                      f"{reason_order}"}


def _by_naming(tbl) -> Optional[dict]:
    machines: List[str] = []
    for row in tbl.rows:
        m = row.get("machine")
        if m and str(m) not in machines:
            machines.append(str(m))
    if len(machines) < 2:
        return None
    parsed = []
    for m in machines:
        hit = _NAME_RE.match(m.strip())
        if not hit:
            return None
        parsed.append((m, hit.group("base").strip(" _-").lower(),
                       int(hit.group("major")),
                       int(hit.group("minor")) if hit.group("minor") else None))
    bases = {p[1] for p in parsed}
    if len(bases) > 1 and len(bases) != len(machines):
        return None
    members: Dict[str, List[str]] = {}
    labels: Dict[Tuple[str, int], str] = {}
    for name, base, major, _minor in parsed:
        key = (base, major)
        sid = labels.get(key)
        if sid is None:
            sid = f"{base or 'stage'}_{major:02d}".upper().strip("_")
            labels[key] = sid
            members[sid] = []
        if name not in members[sid]:
            members[sid].append(name)
    order = [labels[k] for k in sorted(labels, key=lambda k: (k[0], k[1]))]
    parallel = sum(1 for v in members.values() if len(v) > 1)
    return {"method": "naming_pattern", "order": order, "members": members,
            "confidence": 0.7,
            "reason": f"machine names follow an indexed pattern; each index is "
                      f"a stage ({parallel} stage(s) have parallel machines)"}


def _by_first_appearance(tbl) -> dict:
    order: List[str] = []
    members: Dict[str, List[str]] = {}
    for row in tbl.rows:
        m = row.get("machine") or row.get("stage")
        if not m:
            continue
        m = str(m)
        if m not in members:
            members[m] = [m]
            order.append(m)
    return {"method": "first_appearance", "order": order, "members": members,
            "confidence": 0.4,
            "reason": "no stage column and no usable naming pattern; stages "
                      "are the distinct machines in order of first appearance, "
                      "which is a guess you should check"}


# ==========================================================================
def detect_topology(tbl, order: List[str],
                    members: Dict[str, List[str]]) -> dict:
    """Look for evidence that the line is not a simple series."""
    flags: List[dict] = []
    stage_of = {m: sid for sid, ms in members.items() for m in ms}

    succ: Dict[str, set] = {}
    pred: Dict[str, set] = {}
    if tbl.has("next_stage"):
        for row in tbl.rows:
            a, b = row.get("stage") or row.get("machine"), row.get("next_stage")
            if a and b:
                succ.setdefault(str(a), set()).add(str(b))
                pred.setdefault(str(b), set()).add(str(a))
    elif tbl.has("order") and tbl.has("timestamp"):
        routes: Dict[str, List[str]] = {}
        for row in sorted([r for r in tbl.rows if r.get("timestamp")],
                          key=lambda r: r["timestamp"]):
            job = row.get("order")
            sid = stage_of.get(str(row.get("machine")), row.get("stage"))
            if not job or not sid:
                continue
            seq = routes.setdefault(str(job), [])
            if not seq or seq[-1] != sid:
                seq.append(str(sid))
        for seq in routes.values():
            for a, b in zip(seq, seq[1:]):
                succ.setdefault(a, set()).add(b)
                pred.setdefault(b, set()).add(a)

    for node, nxt in succ.items():
        if len(nxt) > 1:
            flags.append({"kind": "branching", "stage": node,
                          "targets": sorted(nxt),
                          "detail": f"{node} feeds {len(nxt)} different stages; "
                                    f"the engine models serial lines only"})
    for node, prv in pred.items():
        if len(prv) > 1:
            flags.append({"kind": "merging", "stage": node,
                          "sources": sorted(prv),
                          "detail": f"{node} is fed by {len(prv)} different "
                                    f"stages; the engine models serial lines "
                                    f"only"})
    for sid, ms in members.items():
        if len(ms) > 1:
            flags.append({"kind": "parallel_machines", "stage": sid,
                          "machines": sorted(ms),
                          "detail": f"{len(ms)} machines run in parallel at "
                                    f"{sid}; this IS representable"})
    non_serial = [f for f in flags if f["kind"] in ("branching", "merging")]
    return {
        "serial": not non_serial,
        "flags": flags,
        "verdict": ("serial line with parallel machines per stage — "
                    "representable" if not non_serial else
                    "the data implies branching or merging routes, which this "
                    "twin cannot represent. The stages below are still built "
                    "in series; treat the result as approximate and say so."),
    }


# ==========================================================================
def propose(tbl, name: str = "Imported line") -> dict:
    """Build an editable mapping proposal from a normalized table."""
    grouping = (_by_stage_column(tbl) or _by_naming(tbl)
                or _by_first_appearance(tbl))
    order, members = grouping["order"], grouping["members"]
    stage_of = {m: sid for sid, ms in members.items() for m in ms}
    estimates = estimate_from_table(tbl, stage_of)
    topology = detect_topology(tbl, order, members)

    items = []
    for i, sid in enumerate(order):
        e = estimates.get(sid, {})
        conf = grouping["confidence"]
        reasons = [grouping["reason"]]
        if not e.get("proc_mean_s"):
            conf *= 0.8
            reasons.append("no cycle-time evidence for this stage")
        elif e.get("proc_provenance") == "observed":
            reasons.append(f"cycle time from {e.get('proc_samples')} observed "
                           f"samples")
        else:
            conf *= 0.9
            reasons.append(f"only {e.get('proc_samples', 0)} cycle-time "
                           f"samples; treated as estimated")
        items.append({
            "order": i, "id": sid, "name": sid,
            "machines": sorted(members.get(sid, []), key=_sort_key),
            "machine_count": max(1, len(members.get(sid, []))),
            "proc_mean_s": e.get("proc_mean_s"),
            "proc_cv": e.get("proc_cv"),
            "mtbf_s": e.get("mtbf_s"), "mttr_s": e.get("mttr_s"),
            "yield": e.get("yield"),
            "samples": {"cycle": e.get("proc_samples", 0),
                        "repair": e.get("mttr_samples", 0),
                        "failures": e.get("mtbf_samples", 0)},
            "provenance": {"proc": e.get("proc_provenance", "reference"),
                           "mtbf": e.get("mtbf_provenance", "reference"),
                           "mttr": e.get("mttr_provenance", "reference"),
                           "yield": e.get("yield_provenance", "reference")},
            "confidence": round(min(0.99, conf), 2),
            "reason": "; ".join(reasons),
            "include": True,
        })

    return {
        "name": name,
        "method": grouping["method"],
        "method_reason": grouping["reason"],
        "stages": items,
        "topology": topology,
        "quality": tbl.quality,
        "columns": [c.to_dict() for c in tbl.columns],
        "kind": tbl.kind,
        "rows": len(tbl.rows),
    }


def validate_proposal(proposal: dict) -> List[str]:
    """Deterministic validation. This has the final say over any suggestion,
    including anything an LLM proposed."""
    errs: List[str] = []
    stages = [s for s in proposal.get("stages", []) if s.get("include", True)]
    if not stages:
        errs.append("no stages selected")
    ids = [str(s.get("id", "")).strip() for s in stages]
    if any(not i for i in ids):
        errs.append("every stage needs an id")
    if len(set(ids)) != len(ids):
        errs.append("stage ids must be unique")
    for s in stages:
        n = s.get("machine_count", 1)
        if not isinstance(n, int) or n < 1:
            errs.append(f"{s.get('id')}: machine count must be a whole number "
                        f">= 1")
        pm = s.get("proc_mean_s")
        if pm is not None and (not isinstance(pm, (int, float)) or pm <= 0):
            errs.append(f"{s.get('id')}: processing time must be > 0")
        y = s.get("yield")
        if y is not None and not (0 < float(y) <= 1):
            errs.append(f"{s.get('id')}: yield must be in (0, 1]")
    return errs


def apply_edits(proposal: dict, edits: List[dict]) -> dict:
    """Apply the user's edits to a proposal. Edits are matched on stage id and
    are recorded so the review screen can show what the user changed."""
    by_id = {s["id"]: s for s in proposal.get("stages", [])}
    changed = []
    for e in edits or []:
        s = by_id.get(str(e.get("id")))
        if s is None:
            continue
        for field in ("name", "machine_count", "proc_mean_s", "proc_cv",
                      "mtbf_s", "mttr_s", "yield", "in_buffer", "include",
                      "order"):
            if field in e and e[field] != s.get(field):
                s[field] = e[field]
                s.setdefault("edited", []).append(field)
                s["provenance"] = dict(s.get("provenance", {}))
                if field in ("proc_mean_s", "proc_cv"):
                    s["provenance"]["proc"] = "estimated"
                changed.append({"id": s["id"], "field": field,
                                "value": e[field]})
    proposal["stages"] = sorted(proposal["stages"],
                                key=lambda s: s.get("order", 0))
    proposal["edits"] = changed
    return proposal


def to_plant(proposal: dict) -> Tuple[Plant, Dict[str, str], List[dict]]:
    """Turn a validated proposal into a Plant plus provenance."""
    errs = validate_proposal(proposal)
    if errs:
        raise ValueError("; ".join(errs))
    stages = [s for s in sorted(proposal["stages"],
                                key=lambda s: s.get("order", 0))
              if s.get("include", True)]
    estimates = {}
    names = {}
    for s in stages:
        names[s["id"]] = s.get("name") or s["id"]
        estimates[s["id"]] = {
            "machines": int(s.get("machine_count", 1)),
            "machine_ids": s.get("machines", []),
            "proc_mean_s": s.get("proc_mean_s"),
            "proc_cv": s.get("proc_cv") or 0.0,
            "proc_provenance": s.get("provenance", {}).get("proc", "reference"),
            "mtbf_s": s.get("mtbf_s"), "mttr_s": s.get("mttr_s"),
            "mtbf_provenance": s.get("provenance", {}).get("mtbf", "reference"),
            "mttr_provenance": s.get("provenance", {}).get("mttr", "reference"),
            "yield": s.get("yield"),
            "yield_provenance": s.get("provenance", {}).get("yield",
                                                            "reference"),
            "in_buffer": s.get("in_buffer", -1),
        }
    plant, prov, notes = build_plant(proposal.get("name", "Imported line"),
                                     [s["id"] for s in stages], estimates,
                                     names)
    if not proposal.get("topology", {}).get("serial", True):
        notes.append({"stage": "*", "field": "topology",
                      "detail": proposal["topology"]["verdict"]})
    perrs = plant.validate()
    if perrs:
        raise ValueError("; ".join(perrs))
    return plant, prov, notes
