"""
Turns raw simulation state-time into the five things the problem statement
asks for: bottlenecks, downtime propagation, throughput loss, resource
utilisation, operational inefficiencies.

Nothing here is heuristic hand-waving -- every number is either an integral of
machine state over time, or a closed-form capacity expression that the
simulation is then checked against.
"""
from __future__ import annotations

import statistics
from typing import Dict, List

from .engine import (BLOCKED, BUSY, DOWN, SETUP, SINK, SOURCE, STARVED,
                     Simulation)
from .model import INF, Plant


# ==========================================================================
# 1. Analytical capacity model (no simulation required)
# ==========================================================================
def capacity_table(plant: Plant) -> List[dict]:
    """Per-stage capacity, degraded step by step.

        r_nom_i    = 3600 * n_i / t_i                       nameplate
        r_avail_i  = 3600 * n_i / (t_i / A_i)               after breakdowns
        r_setup_i  = 3600 * n_i / (t_i / A_i + s_i / b_i)   after changeovers
        r_good_i   = r_setup_i * PROD(y_k, k >= i)          good units to sink

    The line's structural capacity is min_i r_good_i, and argmin is the
    *static* bottleneck. The simulation then tells us how much of that we
    actually get once variability and finite buffers couple the stages.
    """
    rows = []
    for i, s in enumerate(plant.stages):
        A = s.availability()
        r_nom = s.nominal_rate_per_h()
        r_avail = 3600.0 * s.machines / (s.proc.mean / A)
        r_setup = s.capacity_per_h()
        ydown = plant.downstream_yield(i)
        rows.append({
            "id": s.id, "name": s.name, "machines": s.machines,
            "t_mean": s.proc.mean, "cv": s.proc.cv, "availability": A,
            "yield": s.yield_rate, "downstream_yield": ydown,
            "r_nominal": r_nom, "r_after_avail": r_avail,
            "r_after_setup": r_setup, "r_good": r_setup * ydown,
        })
    return rows


# ==========================================================================
# 2. KPI extraction from one simulation run
# ==========================================================================
def kpis(sim: Simulation) -> dict:
    p, T = sim.p, sim.window
    th = 3600.0 * sim.good / T                      # good units / hour
    started = sim.good + sum(sim.scrap)
    wip = sim.wip_area / T
    ct = statistics.mean(sim.cts) if sim.cts else float("nan")
    ct_p95 = (sorted(sim.cts)[int(0.95 * (len(sim.cts) - 1))]
              if len(sim.cts) > 1 else ct)

    stages = []
    for i, s in enumerate(p.stages):
        mt = s.machines * T                          # total machine-seconds
        st = sim.state_time[i]
        row = {
            "id": s.id, "name": s.name,
            "busy": st[BUSY] / mt, "setup": st[SETUP] / mt, "down": st[DOWN] / mt,
            "blocked": st[BLOCKED] / mt, "starved": st[STARVED] / mt,
            "bn_share": sim.bn_time[i] / T,
            "buf_avg": sim.buf_area[i] / T,
            "buf_cap": None if s.in_buffer == INF else s.in_buffer,
            "buf_fill": (sim.buf_area[i] / T / s.in_buffer) if s.in_buffer not in (INF, 0) else None,
            "scrap": sim.scrap[i], "failures": sim.failures[i],
        }
        row["utilisation"] = row["busy"]
        stages.append(row)

    # theoretical minimum flow time = sum of mean processing (+ amortised setup)
    t_theory = sum(s.proc.mean + (s.setup_time / s.batch_size if s.batch_size else 0.0)
                   for s in p.stages)

    return {
        "throughput_per_h": th,
        "good": sim.good,
        "scrap_total": sum(sim.scrap),
        "first_pass_yield": (sim.good / started) if started else float("nan"),
        "wip": wip,
        "cycle_time_s": ct,
        "cycle_time_p95_s": ct_p95,
        "flow_ratio": ct / t_theory if t_theory > 0 else float("nan"),
        "theoretical_flow_s": t_theory,
        "concurrent_active_frac": sim.bn_shift / T,
        "stages": stages,
        "window_s": T,
    }


# ==========================================================================
# 3. Bottleneck ranking
# ==========================================================================
def bottlenecks(sim: Simulation, k: dict) -> List[dict]:
    """Rank stages by a composite score.

    Primary signal is the shifting-bottleneck share (fraction of time the stage
    holds the longest uninterrupted active period). We add the pressure it puts
    on its neighbours -- a true constraint starves what is downstream of it and
    blocks what is upstream of it -- so a stage that is merely busy but never
    constrains anyone does not outrank a genuine constraint.

        score_i = 0.55*bn_share_i + 0.25*(busy+down+setup)_i
                + 0.10*blocked_{i-1} + 0.10*starved_{i+1}
    """
    rows = k["stages"]
    out = []
    for i, r in enumerate(rows):
        up_block = rows[i - 1]["blocked"] if i > 0 else 0.0
        dn_starve = rows[i + 1]["starved"] if i + 1 < len(rows) else 0.0
        active = r["busy"] + r["down"] + r["setup"]
        score = 0.55 * r["bn_share"] + 0.25 * active + 0.10 * up_block + 0.10 * dn_starve
        out.append({**r, "upstream_blocked": up_block,
                    "downstream_starved": dn_starve, "score": score})
    out.sort(key=lambda x: -x["score"])
    return out


# ==========================================================================
# 4. Downtime propagation
# ==========================================================================
def propagation(sim: Simulation) -> dict:
    """Aggregate the idle-time attribution matrix.

    For every second a machine spends STARVED or BLOCKED, the engine has
    already walked the empty/full buffer chain to the stage that caused it.
    Here we roll that up into:

      induced[c][v]       seconds of idle at v caused by c
      downtime_induced[c] seconds of idle elsewhere caused specifically by c
                          being DOWN
      amplification[c]    downtime_induced[c] / own_downtime[c]

    Amplification > 1 means one second of breakdown at c costs the line more
    than one machine-second -- that is downtime propagation, quantified.
    """
    p = sim.p
    n = len(p.stages)
    induced: Dict[tuple, float] = {}
    downtime_induced = [0.0] * n
    own_down = [sim.state_time[i][DOWN] for i in range(n)]

    for (c_si, c_st, v_si, v_st), secs in sim.attrib.items():
        induced[(c_si, v_si, c_st, v_st)] = induced.get((c_si, v_si, c_st, v_st), 0.0) + secs
        if c_st == DOWN and 0 <= c_si < n:
            downtime_induced[c_si] += secs

    rows = []
    for i, s in enumerate(p.stages):
        amp = (downtime_induced[i] / own_down[i]) if own_down[i] > 0 else 0.0
        rows.append({
            "id": s.id, "name": s.name,
            "own_downtime_s": own_down[i],
            "induced_idle_s": downtime_induced[i],
            "amplification": amp,
            "total_cost_s": own_down[i] + downtime_induced[i],
        })
    rows.sort(key=lambda r: -r["total_cost_s"])

    flows = [{"cause": ("SOURCE" if c == SOURCE else "SINK" if c == SINK else p.stages[c].id),
              "victim": p.stages[v].id, "cause_state": cs, "victim_state": vs,
              "seconds": secs}
             for (c, v, cs, vs), secs in induced.items()]
    flows.sort(key=lambda f: -f["seconds"])
    return {"stages": rows, "flows": flows}


# ==========================================================================
# 5. Throughput-loss waterfall
# ==========================================================================
def loss_waterfall(plant: Plant, k: dict, th_override: float | None = None) -> dict:
    """Decompose the gap between nameplate rate and delivered rate.

      L0 = min_i r_nominal_i                  perfect world
      L1 = min_i r_after_avail_i              - breakdown loss
      L2 = min_i r_after_setup_i              - changeover loss
      L3 = min_i r_good_i                     - scrap loss
      TH = simulated good units / hour        - flow loss (variability,
                                                finite buffers, starvation,
                                                blocking)

    The residual L3 - TH is the part no spreadsheet can predict. It is the
    reason this is a simulation and not a capacity calculator.
    """
    cap = capacity_table(plant)
    L0 = min(r["r_nominal"] for r in cap)
    L1 = min(r["r_after_avail"] for r in cap)
    L2 = min(r["r_after_setup"] for r in cap)
    L3 = min(r["r_good"] for r in cap)
    static_bn = min(cap, key=lambda r: r["r_good"])["id"]
    TH = k["throughput_per_h"] if th_override is None else th_override
    buckets = [
        ("Nameplate rate of slowest stage", L0, None),
        ("Breakdown loss", L1, L0 - L1),
        ("Changeover loss", L2, L1 - L2),
        ("Scrap / yield loss", L3, L2 - L3),
        ("Flow loss (variability + blocking + starvation)", TH, L3 - TH),
    ]
    return {"buckets": buckets, "static_bottleneck": static_bn,
            "structural_capacity": L3, "achieved": TH,
            "flow_efficiency": TH / L3 if L3 else float("nan"),
            "overall_efficiency": TH / L0 if L0 else float("nan"),
            "capacity_table": cap}


# ==========================================================================
# 6. Operational inefficiencies + self-validation
# ==========================================================================
def inefficiencies(plant: Plant, k: dict, prop: dict) -> List[dict]:
    out = []
    rows = k["stages"]
    for i, r in enumerate(rows):
        if r["blocked"] > 0.10:
            out.append({"sev": "high" if r["blocked"] > 0.25 else "med", "stage": r["id"],
                        "code": "BLOCKING",
                        "msg": f"{r['blocked']*100:.1f}% of machine time blocked - "
                               f"downstream cannot absorb output; buffer after {r['id']} "
                               f"or capacity at the next stage is the binding limit."})
        if r["starved"] > 0.20 and r["bn_share"] < 0.25:
            out.append({"sev": "med", "stage": r["id"], "code": "STARVATION",
                        "msg": f"{r['starved']*100:.1f}% starved - this stage is paying for "
                               f"an upstream constraint, not its own."})
        if r["buf_fill"] is not None and r["buf_fill"] < 0.20 and r["buf_cap"] and r["buf_cap"] >= 4:
            out.append({"sev": "low", "stage": r["id"], "code": "OVERSIZED_BUFFER",
                        "msg": f"Buffer before {r['id']} averages "
                               f"{r['buf_avg']:.1f}/{r['buf_cap']:.0f} units "
                               f"({r['buf_fill']*100:.0f}% full) - capital tied up in WIP "
                               f"for no throughput gain."})
        if r["buf_fill"] is not None and r["buf_fill"] > 0.85:
            out.append({"sev": "med", "stage": r["id"], "code": "SATURATED_BUFFER",
                        "msg": f"Buffer before {r['id']} runs {r['buf_fill']*100:.0f}% full - "
                               f"it has stopped decoupling and is now just queue delay."})
        if r["down"] > 0.08:
            out.append({"sev": "high" if r["down"] > 0.15 else "med", "stage": r["id"],
                        "code": "AVAILABILITY",
                        "msg": f"{r['down']*100:.1f}% of machine time in breakdown."})

    if k["flow_ratio"] == k["flow_ratio"] and k["flow_ratio"] > 3.0:
        out.append({"sev": "med", "stage": "-", "code": "FLOW_RATIO",
                    "msg": f"Cycle time is {k['flow_ratio']:.1f}x theoretical minimum - "
                           f"units spend {(1-1/k['flow_ratio'])*100:.0f}% of their life waiting."})

    caps = [r for r in capacity_table(plant)]
    lo, hi = min(c["r_good"] for c in caps), max(c["r_good"] for c in caps)
    if hi > 0 and (hi - lo) / hi > 0.25:
        out.append({"sev": "med", "stage": "-", "code": "LINE_IMBALANCE",
                    "msg": f"Stage capacities span {lo:.0f}-{hi:.0f} units/h "
                           f"({(hi-lo)/hi*100:.0f}% spread) - the fast stages are "
                           f"over-specified relative to the constraint."})

    for r in prop["stages"][:1]:
        if r["amplification"] > 1.0:
            out.append({"sev": "high", "stage": r["id"], "code": "PROPAGATION",
                        "msg": f"Each second of downtime at {r['id']} idles "
                               f"{r['amplification']:.2f}s elsewhere in the line."})
    order = {"high": 0, "med": 1, "low": 2}
    out.sort(key=lambda x: order[x["sev"]])
    return out


def validate_run(sim: Simulation, k: dict, cap_tol: float = 0.08) -> List[dict]:
    """Self-checks that prove the twin is internally consistent. If any of
    these fail, do not trust the numbers."""
    checks = []
    # Little's Law: WIP = throughput * cycle time
    th_s = k["throughput_per_h"] / 3600.0
    started_rate = th_s / max(1e-9, k["first_pass_yield"])
    pred = started_rate * k["cycle_time_s"]
    err = abs(pred - k["wip"]) / max(1e-9, k["wip"])
    checks.append({"name": "Little's Law (WIP = lambda x CT)", "value": f"{pred:.2f} vs {k['wip']:.2f}",
                   "err": err, "pass": err < 0.10})
    # state times must sum to the observation window
    worst = 0.0
    for i, s in enumerate(sim.p.stages):
        total = sum(sim.state_time[i].values())
        worst = max(worst, abs(total - s.machines * k["window_s"]) / (s.machines * k["window_s"]))
    checks.append({"name": "Machine-state time conservation", "value": f"max drift {worst*100:.4f}%",
                   "err": worst, "pass": worst < 1e-6})
    # every idle second must be attributed to some cause
    idle = sum(sim.state_time[i][STARVED] + sim.state_time[i][BLOCKED] for i in range(len(sim.p.stages)))
    attributed = sum(sim.attrib.values())
    e = abs(idle - attributed) / max(1e-9, idle)
    checks.append({"name": "Idle-time attribution closure", "value": f"{attributed:.0f}s of {idle:.0f}s",
                   "err": e, "pass": e < 1e-6})
    # simulated throughput must not exceed structural capacity
    cap = min(r["r_good"] for r in capacity_table(sim.p))
    checks.append({"name": "Throughput <= analytical capacity",
                   "value": f"{k['throughput_per_h']:.1f} <= {cap:.1f} (+/-{cap_tol*100:.0f}% single-run sampling)",
                   "err": 0.0, "pass": k["throughput_per_h"] <= cap * (1.0 + cap_tol)})
    return checks


def full_report(sim: Simulation, th_override: float | None = None) -> dict:
    """th_override: pass the mean throughput across replications so the loss
    waterfall is not distorted by single-run sampling noise."""
    k = kpis(sim)
    prop = propagation(sim)
    return {
        "kpis": k,
        "bottlenecks": bottlenecks(sim, k),
        "propagation": prop,
        "losses": loss_waterfall(sim.p, k, th_override),
        "inefficiencies": inefficiencies(sim.p, k, prop),
        "validation": validate_run(sim, k),
    }
