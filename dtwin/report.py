"""Plain-text rendering of the analytics. The API layer serves the same dicts
as JSON; this exists so the engine is demonstrable with zero front-end."""
from __future__ import annotations

from typing import List

from .analysis import full_report
from .engine import Simulation


def _rule(c="-", n=94):
    return c * n


def hdr(title: str) -> str:
    return f"\n{_rule('=')}\n{title}\n{_rule('=')}"


def render_baseline(sim: Simulation, th_override=None, reps: int = 1) -> str:
    R = full_report(sim, th_override)
    k, L = R["kpis"], R["losses"]
    o: List[str] = []

    o.append(hdr(f"BASELINE  |  {sim.p.name}"))
    o.append(f"observation window {k['window_s']/3600:.1f} h after {sim.p.warmup/3600:.1f} h warm-up"
             f"   seed {sim.seed}")
    o.append("")
    th_show = k['throughput_per_h'] if th_override is None else th_override
    src = "this run" if th_override is None else f"mean of {reps} replications"
    o.append(f"  Throughput            {th_show:8.2f} good units/h   ({src})")
    o.append(f"  Units good / scrap    {k['good']:8d} / {k['scrap_total']}")
    o.append(f"  First-pass yield      {k['first_pass_yield']*100:8.2f} %")
    o.append(f"  WIP (avg)             {k['wip']:8.2f} units")
    o.append(f"  Cycle time  mean/p95  {k['cycle_time_s']:8.1f} s / {k['cycle_time_p95_s']:.1f} s")
    o.append(f"  Flow ratio            {k['flow_ratio']:8.2f} x theoretical minimum "
             f"({k['theoretical_flow_s']:.0f}s)")

    o.append(hdr("RESOURCE UTILISATION  (fraction of machine-time, per stage)"))
    o.append(f"{'stage':<24}{'M':>3}{'busy':>8}{'setup':>8}{'down':>8}{'blocked':>9}"
             f"{'starved':>9}{'BN share':>10}{'buffer':>12}")
    o.append(_rule())
    for r in k["stages"]:
        buf = ("inf" if r["buf_cap"] is None
               else f"{r['buf_avg']:.1f}/{r['buf_cap']:.0f}")
        o.append(f"{(r['id']+' '+r['name'])[:23]:<24}"
                 f"{next(s.machines for s in sim.p.stages if s.id==r['id']):>3}"
                 f"{r['busy']*100:>7.1f}%{r['setup']*100:>7.1f}%{r['down']*100:>7.1f}%"
                 f"{r['blocked']*100:>8.1f}%{r['starved']*100:>8.1f}%{r['bn_share']*100:>9.1f}%"
                 f"{buf:>12}")

    o.append(hdr("BOTTLENECK RANKING"))
    o.append("score = 0.55*BN-share + 0.25*active + 0.10*upstream-blocked + 0.10*downstream-starved")
    o.append(f"{'#':<3}{'stage':<24}{'score':>8}{'BN share':>10}{'util':>8}"
             f"{'up blocked':>12}{'down starved':>14}")
    o.append(_rule())
    for i, b in enumerate(R["bottlenecks"], 1):
        o.append(f"{i:<3}{(b['id']+' '+b['name'])[:23]:<24}{b['score']:>8.3f}{b['bn_share']*100:>9.1f}%"
                 f"{b['utilisation']*100:>7.1f}%{b['upstream_blocked']*100:>11.1f}%"
                 f"{b['downstream_starved']*100:>13.1f}%")
    top, second = R["bottlenecks"][0], R["bottlenecks"][1]
    o.append(f"\n  PRIMARY CONSTRAINT: {top['id']} {top['name']} -- momentary bottleneck "
             f"{top['bn_share']*100:.1f}% of the shift "
             f"(next: {second['id']} at {second['bn_share']*100:.1f}%)")

    o.append(hdr("THROUGHPUT LOSS WATERFALL  (units/h)"))
    o.append(f"  static capacity model says the constraint is {L['static_bottleneck']}")
    o.append("")
    for name, level, loss in L["buckets"]:
        if loss is None:
            o.append(f"  {name:<52}{level:>9.2f}")
        else:
            o.append(f"  {name:<52}{level:>9.2f}   -{loss:>7.2f}  ({loss/L['buckets'][0][1]*100:>5.1f}% of nameplate)")
    o.append("")
    o.append(f"  Flow efficiency (achieved / structural capacity)   {L['flow_efficiency']*100:6.1f} %")
    o.append(f"  Overall efficiency (achieved / nameplate)          {L['overall_efficiency']*100:6.1f} %")

    o.append(hdr("DOWNTIME PROPAGATION  (who pays for whose stoppage)"))
    o.append(f"{'stage':<24}{'own downtime':>14}{'idle induced':>14}{'amplification':>15}{'total cost':>13}")
    o.append(_rule())
    for r in R["propagation"]["stages"]:
        if r["own_downtime_s"] <= 0 and r["induced_idle_s"] <= 0:
            continue
        o.append(f"{(r['id']+' '+r['name'])[:23]:<24}{r['own_downtime_s']:>13.0f}s{r['induced_idle_s']:>13.0f}s"
                 f"{r['amplification']:>14.2f}x{r['total_cost_s']:>12.0f}s")
    o.append("\n  top idle flows (cause -> victim):")
    for f in R["propagation"]["flows"][:8]:
        o.append(f"    {f['cause']:>6} [{f['cause_state']:<8}]  ->  {f['victim']:<4} "
                 f"[{f['victim_state']:<7}]  {f['seconds']:>9.0f}s")

    o.append(hdr("OPERATIONAL INEFFICIENCIES"))
    if not R["inefficiencies"]:
        o.append("  none detected")
    for d in R["inefficiencies"]:
        o.append(f"  [{d['sev'].upper():<4}] {d['code']:<18} {d['stage']:<4} {d['msg']}")

    o.append(hdr("SELF-VALIDATION"))
    for c in R["validation"]:
        o.append(f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['name']:<42} {c['value']}")
    return "\n".join(o)


def render_comparison(cmp: dict, title: str) -> str:
    o = [hdr(f"WHAT-IF  |  {title}")]
    o.append("  change: " + "; ".join(cmp["patches"]))
    o.append(f"  paired replications: {len(cmp['seeds'])} common-random-number seeds")
    o.append("")
    o.append(f"{'metric':<26}{'baseline':>12}{'scenario':>12}{'delta':>12}{'%':>9}"
             f"{'95% CI':>22}{'sig':>6}")
    o.append(_rule())
    for m, d in cmp["deltas"].items():
        ci = f"[{d['ci_low']:+.2f}, {d['ci_high']:+.2f}]"
        o.append(f"{m:<26}{d['base']:>12.2f}{d['scenario']:>12.2f}{d['delta']:>+12.2f}"
                 f"{d['pct']:>+8.1f}%{ci:>22}{'YES' if d['significant'] else 'no':>6}")
    o.append("")
    o.append(f"{'stage':<8}{'util':>20}{'blocked':>20}{'starved':>20}{'BN share':>20}")
    o.append(_rule())
    for s in cmp["stage_shift"]:
        o.append(f"{s['id']:<8}"
                 f"{s['util']['base']*100:>9.1f}%->{s['util']['scenario']*100:>6.1f}%"
                 f"{s['blocked']['base']*100:>10.1f}%->{s['blocked']['scenario']*100:>6.1f}%"
                 f"{s['starved']['base']*100:>10.1f}%->{s['starved']['scenario']*100:>6.1f}%"
                 f"{s['bn_share']['base']*100:>10.1f}%->{s['bn_share']['scenario']*100:>6.1f}%")
    o.append("")
    if cmp["bottleneck_migrated"]:
        o.append(f"  >> BOTTLENECK MIGRATED: {cmp['bottleneck_before']} -> {cmp['bottleneck_after']}")
    else:
        o.append(f"  >> constraint unchanged: {cmp['bottleneck_before']}")
    th = cmp["deltas"]["throughput_per_h"]
    if not th["significant"]:
        o.append("  >> throughput change is NOT statistically distinguishable from run-to-run "
                 "noise at 95%.")
    return "\n".join(o)


def render_ranking(rows: List[dict], top: int = 12) -> str:
    o = [hdr("PRESCRIPTIVE RANKING  |  every single-lever intervention, ranked by throughput gained")]
    o.append(f"{'#':<3}{'stage':<6}{'intervention':<26}{'d TH/h':>10}{'%':>8}"
             f"{'95% CI':>20}{'capex':>12}{'units/h per 100k':>18}")
    o.append(_rule())
    for i, r in enumerate(rows[:top], 1):
        ci = f"[{r['ci_low']:+.2f},{r['ci_high']:+.2f}]"
        gpc = "n/a" if r["cost"] <= 0 else f"{r['delta_th']/r['cost']*1e5:.2f}"
        cost = "free" if r["cost"] <= 0 else f"{r['cost']:,.0f}"
        o.append(f"{i:<3}{r['stage']:<6}{r['label']:<26}{r['delta_th']:>+10.2f}{r['pct']:>+7.1f}%"
                 f"{ci:>20}{cost:>12}{gpc:>18}")
    if not rows:
        o.append("  no intervention produced a statistically significant gain")
    return "\n".join(o)
