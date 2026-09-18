"""
The what-if engine. This is the part the problem statement calls the key
challenge, so it is treated as a first-class subsystem rather than a re-run
button.

Three ideas do the work:

1. A scenario is a *patch*, not a copy. `[{"stage":"S3","field":"proc.mean",
   "op":"mul","value":1.2}]` is a portable, loggable, diffable object. The UI
   never mutates the baseline.

2. A single re-run is worthless. One replication of a stochastic line has a
   throughput standard error of several units/hour; a naive before/after diff
   reports noise as insight. We run N paired replications under *common random
   numbers*: scenario k and the baseline use identical seeds, and because RNG
   streams are keyed per stage, changing stage 3 does not disturb the random
   draws at stage 1. The per-seed differences d_j = KPI_scenario,j -
   KPI_base,j are then paired, which cancels most of the shared variance.

3. Every delta is reported with a paired-t 95% confidence interval. If the
   interval contains zero we say so out loud instead of pretending a 0.4 unit/h
   move is a win.

On top of that sits the prescriptive layer: instead of waiting for a human to
guess which knob to turn, `sweep_*` perturbs every stage one at a time and
ranks the interventions by throughput gained (and by gain per unit of capex).
That is what makes this a decision system rather than a visualiser.
"""
from __future__ import annotations

import copy
import math
import statistics
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .analysis import bottlenecks, kpis, loss_waterfall
from .engine import Simulation
from .model import INF, Dist, Plant

# two-sided 95% critical values; falls back to the normal approximation
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
        8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160,
        14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093,
        20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
        26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}


def t95(df: int) -> float:
    return _T95.get(df, 1.960)


# ==========================================================================
# Patches
# ==========================================================================
PATCHABLE = {
    "proc.mean", "proc.cv", "machines", "in_buffer", "mtbf", "repair.mean",
    "yield_rate", "setup_time", "batch_size",
}


@dataclass
class Patch:
    stage: str
    field: str
    op: str = "set"      # set | mul | add
    value: float = 0.0

    def describe(self) -> str:
        sym = {"set": "=", "mul": "x", "add": "+"}[self.op]
        return f"{self.stage}.{self.field} {sym} {self.value:g}"


def _get(stage, field: str):
    if "." in field:
        a, b = field.split(".", 1)
        return getattr(getattr(stage, a), b)
    return getattr(stage, field)


def _set(stage, field: str, val):
    if field in ("machines", "batch_size"):
        val = max(1, int(round(val))) if field == "machines" else int(round(val))
    if field == "in_buffer":
        val = INF if val < 0 else float(int(round(val)))
    if "." in field:
        a, b = field.split(".", 1)
        setattr(getattr(stage, a), b, float(val) if not isinstance(val, int) else val)
    else:
        setattr(stage, field, val)


def apply_patches(plant: Plant, patches: List[Patch]) -> Plant:
    """Return a NEW plant with the patches applied. The baseline is never
    mutated, so a scenario tree can be explored without state leakage."""
    p = copy.deepcopy(plant)
    for patch in patches:
        if patch.field not in PATCHABLE:
            raise ValueError(f"field {patch.field!r} is not patchable; "
                             f"allowed: {sorted(PATCHABLE)}")
        s = p.stages[p.index(patch.stage)]
        cur = _get(s, patch.field)
        cur = 0.0 if cur == INF and patch.op != "set" else cur
        new = (patch.value if patch.op == "set" else
               cur * patch.value if patch.op == "mul" else
               cur + patch.value)
        _set(s, patch.field, new)
    errs = p.validate()
    if errs:
        raise ValueError("patched plant is invalid: " + "; ".join(errs))
    return p


def patches_from_json(items: List[dict]) -> List[Patch]:
    return [Patch(i["stage"], i["field"], i.get("op", "set"), float(i["value"]))
            for i in items]


# ==========================================================================
# Replication
# ==========================================================================
def run_once(plant: Plant, seed: int) -> dict:
    sim = Simulation(plant, seed=seed).run()
    k = kpis(sim)
    bn = bottlenecks(sim, k)
    k["_bn_top"] = bn[0]["id"]
    k["_sim"] = sim
    return k


def replicate(plant: Plant, seeds: List[int]) -> List[dict]:
    return [run_once(plant, s) for s in seeds]


def _scalar(k: dict, metric: str) -> float:
    if metric.startswith("stage:"):
        _, sid, field = metric.split(":")
        for r in k["stages"]:
            if r["id"] == sid:
                return r[field]
        raise KeyError(metric)
    return k[metric]


METRICS = ["throughput_per_h", "wip", "cycle_time_s", "cycle_time_p95_s",
           "first_pass_yield", "flow_ratio"]
BIGGER_IS_BETTER = {"throughput_per_h": True, "wip": False, "cycle_time_s": False,
                    "cycle_time_p95_s": False, "first_pass_yield": True,
                    "flow_ratio": False}


def paired_delta(base: List[dict], scen: List[dict], metric: str) -> dict:
    """Paired-t on common random numbers. Returns mean delta, 95% CI and
    significance."""
    b = [_scalar(k, metric) for k in base]
    s = [_scalar(k, metric) for k in scen]
    d = [x - y for x, y in zip(s, b)]
    n = len(d)
    mean = statistics.mean(d)
    sd = statistics.stdev(d) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else 0.0
    half = t95(n - 1) * se
    base_mean = statistics.mean(b)
    return {
        "metric": metric,
        "base": base_mean,
        "scenario": statistics.mean(s),
        "delta": mean,
        "pct": (mean / base_mean * 100.0) if base_mean else float("nan"),
        "ci_low": mean - half, "ci_high": mean + half,
        "significant": (mean - half) * (mean + half) > 0 and n > 1,
        "n": n,
    }


def compare(plant: Plant, patches: List[Patch], seeds: List[int],
            metrics: Optional[List[str]] = None,
            base_runs: Optional[List[dict]] = None) -> dict:
    """Full before/after report for one scenario."""
    metrics = metrics or METRICS
    base = base_runs if base_runs is not None else replicate(plant, seeds)
    scen_plant = apply_patches(plant, patches)
    scen = replicate(scen_plant, seeds)

    deltas = [paired_delta(base, scen, m) for m in metrics]

    # per-stage utilisation shift and bottleneck migration
    stage_shift = []
    for row in base[0]["stages"]:
        sid = row["id"]
        stage_shift.append({
            "id": sid,
            "util": paired_delta(base, scen, f"stage:{sid}:utilisation"),
            "blocked": paired_delta(base, scen, f"stage:{sid}:blocked"),
            "starved": paired_delta(base, scen, f"stage:{sid}:starved"),
            "bn_share": paired_delta(base, scen, f"stage:{sid}:bn_share"),
        })

    def _mode(vals):
        return max(set(vals), key=vals.count)

    bn_before = _mode([k["_bn_top"] for k in base])
    bn_after = _mode([k["_bn_top"] for k in scen])

    return {
        "patches": [p.describe() for p in patches],
        "seeds": seeds,
        "deltas": {d["metric"]: d for d in deltas},
        "stage_shift": stage_shift,
        "bottleneck_before": bn_before,
        "bottleneck_after": bn_after,
        "bottleneck_migrated": bn_before != bn_after,
        "base_runs": base,
        "scenario_runs": scen,
        "scenario_plant": scen_plant,
    }


# ==========================================================================
# Prescriptive sweeps
# ==========================================================================
def sweep(plant: Plant, seeds: List[int],
          builder: Callable[[str], List[Patch]],
          label: Callable[[str], str],
          cost: Callable[[Plant, str], float],
          base_runs: Optional[List[dict]] = None) -> List[dict]:
    """Apply the same class of intervention to every stage in turn and rank by
    throughput gained. This is the automated constraint search: no human has to
    guess which stage to poke."""
    base = base_runs if base_runs is not None else replicate(plant, seeds)
    out = []
    for s in plant.stages:
        try:
            patches = builder(s.id)
            scen_plant = apply_patches(plant, patches)
        except ValueError:
            continue
        scen = replicate(scen_plant, seeds)
        d = paired_delta(base, scen, "throughput_per_h")
        c = cost(plant, s.id)
        out.append({
            "stage": s.id, "name": s.name, "label": label(s.id),
            "delta_th": d["delta"], "pct": d["pct"],
            "ci_low": d["ci_low"], "ci_high": d["ci_high"],
            "significant": d["significant"],
            "cost": c,
            "gain_per_cost": (d["delta"] / c) if c > 0 else float("inf"),
        })
    out.sort(key=lambda r: -r["delta_th"])
    return out


def sweep_speed(plant: Plant, seeds: List[int], pct: float = 0.20,
                base_runs=None) -> List[dict]:
    """What if each stage were `pct` faster?"""
    return sweep(
        plant, seeds,
        builder=lambda sid: [Patch(sid, "proc.mean", "mul", 1.0 / (1.0 + pct))],
        label=lambda sid: f"{pct*100:.0f}% faster cycle time",
        cost=lambda p, sid: p.stages[p.index(sid)].capex_per_machine * 0.5,
        base_runs=base_runs)


def sweep_buffer(plant: Plant, seeds: List[int], add: int = 5,
                 base_runs=None) -> List[dict]:
    """What if each buffer got `add` more slots?"""
    def build(sid):
        st = plant.stages[plant.index(sid)]
        if st.in_buffer == INF:
            raise ValueError("infinite buffer")
        return [Patch(sid, "in_buffer", "add", add)]
    return sweep(plant, seeds, builder=build,
                 label=lambda sid: f"+{add} buffer slots upstream",
                 cost=lambda p, sid: p.stages[p.index(sid)].capex_per_buffer_slot * add,
                 base_runs=base_runs)


def sweep_reliability(plant: Plant, seeds: List[int], mttr_cut: float = 0.5,
                      base_runs=None) -> List[dict]:
    """What if repair time at each stage were halved (better maintenance)?"""
    def build(sid):
        st = plant.stages[plant.index(sid)]
        if st.mtbf == INF:
            raise ValueError("no failures modelled")
        return [Patch(sid, "repair.mean", "mul", mttr_cut)]
    return sweep(plant, seeds, builder=build,
                 label=lambda sid: f"MTTR cut {int((1-mttr_cut)*100)}%",
                 cost=lambda p, sid: 0.0, base_runs=base_runs)


def sweep_machine(plant: Plant, seeds: List[int], base_runs=None) -> List[dict]:
    """What if we added one machine at each stage?"""
    return sweep(plant, seeds,
                 builder=lambda sid: [Patch(sid, "machines", "add", 1)],
                 label=lambda sid: "+1 parallel machine",
                 cost=lambda p, sid: p.stages[p.index(sid)].capex_per_machine,
                 base_runs=base_runs)


def rank_interventions(plant: Plant, seeds: List[int], base_runs=None) -> List[dict]:
    """Run every sweep family and return one global ranked list."""
    base = base_runs if base_runs is not None else replicate(plant, seeds)
    allr = []
    allr += sweep_speed(plant, seeds, base_runs=base)
    allr += sweep_buffer(plant, seeds, base_runs=base)
    allr += sweep_reliability(plant, seeds, base_runs=base)
    allr += sweep_machine(plant, seeds, base_runs=base)
    allr = [r for r in allr if r["significant"] and r["delta_th"] > 0]
    allr.sort(key=lambda r: -r["delta_th"])
    return allr
