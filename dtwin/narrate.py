"""
Narration -- plain English over computed numbers.

One rule, and it is absolute: **the narrator never produces a number.** It
receives computed results and arranges them into sentences. Every figure in the
output must already exist in the input.

That rule is enforced, not merely stated. `explain()` runs any LLM response
through `verify_numbers()`, which extracts every numeric literal and checks it
appears in the source data. A response that invents a figure is discarded and
the deterministic template is used instead.

The templates are the default path, not the fallback-of-shame. The system is
fully functional, offline, with no API key and no network. An LLM, if
configured, only rephrases. That ordering is deliberate: a demo that depends on
a third-party API over conference wifi is a demo that fails.
"""
from __future__ import annotations

import json
import os
import re
from typing import List, Optional

NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def verify_numbers(text: str, source: dict, tol: float = 0.011) -> List[str]:
    """Return the list of numbers in `text` that do not appear in `source`.

    Matching is tolerant of rounding: a claimed 92.1 is accepted if 92.11 is in
    the data. Small integers (0-100) are exempt because they legitimately appear
    as ordinals, counts and percentages derived by trivial arithmetic.
    """
    haystack = _collect_numbers(source)
    bad = []
    for tok in NUM_RE.findall(text):
        v = float(tok)
        if v.is_integer() and 0 <= abs(v) <= 100:
            continue
        if not any(abs(v - h) <= max(tol, abs(h) * 0.005) for h in haystack):
            bad.append(tok)
    return bad


def _collect_numbers(obj, out: Optional[List[float]] = None) -> List[float]:
    if out is None:
        out = []
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        v = float(obj)
        out.append(v)
        out.append(round(v, 1))
        # a fraction legitimately appears in prose as a percentage; anything
        # already larger than 1 does not, so we do not widen the net for it
        if 0.0 <= v <= 1.0:
            out.append(v * 100.0)
            out.append(round(v * 100.0, 1))
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_numbers(v, out)
    return out


# --------------------------------------------------------------------------
# Deterministic templates
# --------------------------------------------------------------------------
def narrate_bottleneck(report: dict) -> str:
    bn = report["bottlenecks"][0]
    prop = next((r for r in report["propagation"]["stages"] if r["id"] == bn["id"]),
                None)
    losses = report["losses"]
    parts = [
        f"{bn['name']} ({bn['id']}) is the constraint. It holds the momentary "
        f"bottleneck for {bn['bn_share']*100:.0f}% of the shift, meaning it is the "
        f"stage that is almost never interrupted while everything else waits on it."
    ]
    if bn["upstream_blocked"] > 0.05:
        parts.append(
            f"Upstream machines sit blocked {bn['upstream_blocked']*100:.0f}% of the "
            f"time because they finish work {bn['id']} cannot accept.")
    if bn["downstream_starved"] > 0.05:
        parts.append(
            f"Downstream sits starved {bn['downstream_starved']*100:.0f}% of the time "
            f"waiting for it.")
    if prop and prop["amplification"] > 1.0:
        parts.append(
            f"Its breakdowns are contagious: each second {bn['id']} is down idles "
            f"{prop['amplification']:.2f} further seconds elsewhere on the line.")
    parts.append(
        f"The line delivers {losses['achieved']:.1f} units an hour against a "
        f"structural capacity of {losses['structural_capacity']:.1f}.")
    return " ".join(parts)


def narrate_stage(report: dict, stage_id: str) -> str:
    row = next((r for r in report["kpis"]["stages"] if r["id"] == stage_id), None)
    if row is None:
        return f"No stage {stage_id} in this line."
    prop = next((r for r in report["propagation"]["stages"] if r["id"] == stage_id),
                None)
    dominant = max(
        [("producing", row["busy"]), ("in changeover", row["setup"]),
         ("broken down", row["down"]),
         ("blocked by the next stage", row["blocked"]),
         ("starved by the previous stage", row["starved"])],
        key=lambda x: x[1])
    out = [f"{row['name']} spends {row['busy']*100:.0f}% of its machine time "
           f"producing."]
    if dominant[0] != "producing":
        out.append(f"Its largest single loss is time {dominant[0]}, at "
                   f"{dominant[1]*100:.0f}%.")
    if row["down"] > 0.02:
        out.append(f"It broke down {row['failures']} times and lost "
                   f"{row['down']*100:.0f}% of capacity to repair.")
    if row["scrap"] > 0:
        out.append(f"It scrapped {row['scrap']} units.")
    if prop and prop["amplification"] > 0.5:
        out.append(f"Each second of its downtime costs the line "
                   f"{prop['amplification']:.2f} seconds of idleness elsewhere.")
    if row["bn_share"] > 0.3:
        out.append("This is the line's constraint.")
    elif row["starved"] > 0.2:
        out.append("It is not the constraint; it is waiting on one.")
    return " ".join(out)


def narrate_scenario(cmp: dict) -> str:
    th = cmp["deltas"]["throughput_per_h"]
    change = "; ".join(cmp["patches"])
    if not th["significant"]:
        s = (f"Changing {change} moved throughput by {th['delta']:+.2f} units an "
             f"hour, but the 95% confidence interval spans zero "
             f"({th['ci_low']:+.2f} to {th['ci_high']:+.2f}). That is within "
             f"run-to-run noise, so this change did nothing measurable.")
        if not cmp["bottleneck_migrated"]:
            s += (f" The constraint is still {cmp['bottleneck_before']}, which is "
                  f"why: effort spent away from the constraint does not move the "
                  f"line.")
        return s
    direction = "lifts" if th["delta"] > 0 else "costs"
    s = (f"Changing {change} {direction} throughput by {abs(th['delta']):.2f} units "
         f"an hour ({th['pct']:+.1f}%), with a 95% confidence interval of "
         f"{th['ci_low']:+.2f} to {th['ci_high']:+.2f} across "
         f"{th['n']} paired replications.")
    ct = cmp["deltas"].get("cycle_time_s")
    if ct and ct["significant"]:
        s += (f" Cycle time moves {ct['delta']:+.0f} seconds "
              f"({ct['pct']:+.1f}%).")
    wip = cmp["deltas"].get("wip")
    if wip and wip["significant"] and abs(wip["pct"]) > 10:
        s += f" Work in progress changes {wip['pct']:+.0f}%."
    if cmp["bottleneck_migrated"]:
        s += (f" The constraint moves from {cmp['bottleneck_before']} to "
              f"{cmp['bottleneck_after']} -- fixing this bottleneck creates the "
              f"next one, and that is where the following investment goes.")
    else:
        s += f" The constraint remains {cmp['bottleneck_before']}."
    return s


def narrate_ranking(ranked: List[dict]) -> str:
    if not ranked:
        return ("No single-lever intervention produced a statistically significant "
                "throughput gain. The line is balanced; the next move is a "
                "multi-stage change, not a single knob.")
    top = ranked[0]
    free = [r for r in ranked if r["cost"] <= 0]
    s = (f"Of the interventions searched, {top['label'].lower()} at {top['stage']} "
         f"gives the largest gain: {top['delta_th']:+.2f} units an hour "
         f"({top['pct']:+.1f}%).")
    if free:
        f0 = free[0]
        s += (f" The best zero-capital move is {f0['label'].lower()} at "
              f"{f0['stage']}, worth {f0['delta_th']:+.2f} units an hour for no "
              f"investment.")
    paid = [r for r in ranked if r["cost"] > 0]
    if paid:
        best_roi = max(paid, key=lambda r: r["gain_per_cost"])
        if best_roi["stage"] != top["stage"] or best_roi["label"] != top["label"]:
            s += (f" On return per unit of capital, {best_roi['label'].lower()} at "
                  f"{best_roi['stage']} is the better buy.")
    return s


def narrate_executive(report: dict, ranked: List[dict]) -> str:
    k, losses = report["kpis"], report["losses"]
    bn = report["bottlenecks"][0]
    buckets = {name: loss for name, level, loss in losses["buckets"] if loss}
    biggest = max(buckets.items(), key=lambda x: x[1]) if buckets else None
    p1 = (f"The line delivers {losses['achieved']:.1f} good units an hour. Its "
          f"slowest stage could nominally run at {losses['buckets'][0][1]:.1f}, so "
          f"{(1-losses['overall_efficiency'])*100:.0f}% of design capacity is "
          f"being lost.")
    if biggest:
        p1 += (f" The largest single loss is {biggest[0].lower()}, worth "
               f"{biggest[1]:.1f} units an hour.")
    p1 += (f" Units spend {(1-1/max(k['flow_ratio'],1.001))*100:.0f}% of their time "
           f"in the line waiting rather than being worked on.")
    p2 = (f"{bn['name']} is the constraint and every improvement should start "
          f"there. ")
    p2 += narrate_ranking(ranked)
    return p1 + "\n\n" + p2


TEMPLATES = {
    "bottleneck": narrate_bottleneck,
    "stage": narrate_stage,
    "scenario": narrate_scenario,
    "ranking": narrate_ranking,
    "executive": narrate_executive,
}


# --------------------------------------------------------------------------
# Optional LLM rephrasing
# --------------------------------------------------------------------------
SYSTEM_RULE = (
    "You are given computed production-line simulation results as JSON, plus a "
    "factually correct draft. Rewrite the draft for a plant manager. Rules: use "
    "ONLY numbers that appear in the JSON or the draft; never estimate or invent "
    "a figure; if a change is marked significant=false you must say it is within "
    "run-to-run noise; maximum 4 sentences; plain language, no jargon, no "
    "bullet points."
)


def explain(mode: str, context: dict, draft: Optional[str] = None,
            use_llm: Optional[bool] = None) -> dict:
    """Produce narration. Returns {text, source, rejected}.

    `source` is "template" or "llm". If an LLM response fails number
    verification it is rejected and the template is returned, with the offending
    tokens listed so the failure is visible rather than silent.
    """
    if draft is None:
        fn = TEMPLATES.get(mode)
        draft = fn(context) if fn else json.dumps(context)[:400]

    if use_llm is None:
        use_llm = bool(os.environ.get("GEMINI_API_KEY"))
    if not use_llm:
        return {"text": draft, "source": "template", "rejected": []}

    try:
        raw = _call_llm(SYSTEM_RULE, json.dumps(context, default=str), draft)
    except Exception as exc:                       # network, key, quota, anything
        return {"text": draft, "source": "template", "rejected": [],
                "note": f"LLM unavailable ({type(exc).__name__}); used template."}

    bad = verify_numbers(raw, {"ctx": context, "draft_numbers":
                               [float(t) for t in NUM_RE.findall(draft)]})
    if bad:
        return {"text": draft, "source": "template", "rejected": bad,
                "note": "LLM response contained figures not present in the data "
                        "and was discarded."}
    return {"text": raw.strip(), "source": "llm", "rejected": []}


def _call_llm(system: str, payload: str, draft: str) -> str:
    """Provider hook: Gemini via its REST API (dtwin/llm.py, urllib only).

    The guard above still decides whether the answer is usable: any figure the
    model invents that is not in the data gets the response rejected and the
    deterministic template returned instead.
    """
    from dtwin import llm
    return llm.generate(system, f"DATA:\n{payload}\n\nDRAFT:\n{draft}",
                        max_tokens=400)
