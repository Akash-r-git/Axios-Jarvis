"""
Calibration -- the step that earns the words "digital twin".

A simulation becomes a twin when it has a measured error against the real thing.
Given one observed shift, this module answers two questions:

    1. How wrong is the twin right now?  (fidelity report)
    2. What parameters would make it right?  (fitting)

An identifiability result that shapes the whole method
------------------------------------------------------
Busy fraction alone CANNOT determine processing times. For stage i,

    u_i = (units through i) x t_i / (n_i x T)

and units through i is set by line throughput. Scale every t_i by a common
factor s and throughput scales by 1/s, so u_i is unchanged. Utilisation is
exactly degenerate under uniform time scaling.

So utilisation fixes the RATIOS between stages, and throughput fixes the SCALE.
The fit alternates the two, and neither alone is sufficient. (This is why a
calibration built only on utilisation will look like it converged -- worst-stage
error near zero -- while every parameter is off by the same multiplicative
factor. We hit exactly that and it is why the throughput step exists.)

Fitting method: damped fixed-point iteration on per-stage processing time.

    A stage's busy fraction is  u_i = (units through i) x t_i / (n_i x T).
    Units through i is set by line throughput, which we cannot change directly,
    so at fixed throughput u_i is proportional to t_i. That gives the update

        t_i  <-  t_i x (u_obs_i / u_sim_i)

    applied with damping factor d in (0,1] because changing t_i also moves
    throughput, which moves every other stage's u. Damping keeps the coupled
    system from oscillating. Each iteration then applies a global scale

        t_i  <-  t_i x (TH_simulated / TH_observed)

    which is the only term that can fix the absolute time scale. We iterate
    until both the worst per-stage utilisation error and the throughput error
    are under tolerance, and we report the full error trajectory so the
    convergence is auditable rather than a black box.

This is a deliberately simple estimator. It is not maximum likelihood, and we
say so. What it gives you is a defensible, inspectable path from "our archetype
guessed 55 s" to "your line actually runs 58.3 s", with the error at every step.
"""
from __future__ import annotations

import copy
import csv
import statistics
from typing import Dict, List, Optional

from .model import Plant
from .whatif import replicate


# --------------------------------------------------------------------------
# Observation format
# --------------------------------------------------------------------------
def load_observation(path: str) -> dict:
    """Read an observed shift from CSV.

    Expected columns: stage_id, busy_fraction, down_fraction, units_out
    Plus one row with stage_id == "_LINE_" carrying shift_hours and good_units.

    This is the shape an MES export or a manual shift log can actually produce.
    Anything richer is a bonus; anything less is not enough to calibrate.
    """
    stages: Dict[str, dict] = {}
    line = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            sid = row["stage_id"].strip()
            if sid == "_LINE_":
                line = {"shift_hours": float(row["shift_hours"]),
                        "good_units": float(row["good_units"])}
            else:
                stages[sid] = {
                    "busy": float(row["busy_fraction"]),
                    "down": float(row.get("down_fraction", 0) or 0),
                    "units_out": float(row.get("units_out", 0) or 0),
                }
    if not line:
        raise ValueError("observation file needs a _LINE_ row with shift_hours "
                         "and good_units")
    line["throughput_per_h"] = line["good_units"] / line["shift_hours"]
    return {"line": line, "stages": stages}


def write_observation(path: str, plant: Plant, seeds: List[int]) -> dict:
    """Generate an observed shift by running a plant and recording what a real
    MES would have logged. Used to produce the demo's ground-truth data."""
    runs = replicate(plant, seeds)
    th = statistics.mean(r["throughput_per_h"] for r in runs)
    hours = plant.horizon / 3600.0
    rows = [{"stage_id": "_LINE_", "busy_fraction": "", "down_fraction": "",
             "units_out": "", "shift_hours": f"{hours:.2f}",
             "good_units": f"{th*hours:.1f}"}]
    for i, s in enumerate(plant.stages):
        busy = statistics.mean(r["stages"][i]["busy"] for r in runs)
        down = statistics.mean(r["stages"][i]["down"] for r in runs)
        rows.append({"stage_id": s.id, "busy_fraction": f"{busy:.4f}",
                     "down_fraction": f"{down:.4f}",
                     "units_out": f"{th*hours:.1f}",
                     "shift_hours": "", "good_units": ""})
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["stage_id", "busy_fraction",
                                           "down_fraction", "units_out",
                                           "shift_hours", "good_units"])
        w.writeheader()
        w.writerows(rows)
    return {"path": path, "throughput_per_h": th}


# --------------------------------------------------------------------------
# Fidelity report
# --------------------------------------------------------------------------
def fidelity(plant: Plant, observation: dict, seeds: List[int]) -> dict:
    """How far is the twin from the observed shift, right now?"""
    runs = replicate(plant, seeds)
    th_sim = statistics.mean(r["throughput_per_h"] for r in runs)
    th_obs = observation["line"]["throughput_per_h"]

    rows = []
    worst = 0.0
    for i, s in enumerate(plant.stages):
        obs = observation["stages"].get(s.id)
        if obs is None:
            continue
        u_sim = statistics.mean(r["stages"][i]["busy"] for r in runs)
        d_sim = statistics.mean(r["stages"][i]["down"] for r in runs)
        err = abs(u_sim - obs["busy"]) / max(1e-6, obs["busy"])
        worst = max(worst, err)
        rows.append({
            "id": s.id, "name": s.name,
            "busy_sim": u_sim, "busy_obs": obs["busy"],
            "busy_err_pct": err * 100.0,
            "down_sim": d_sim, "down_obs": obs["down"],
            "proc_mean": s.proc.mean,
        })

    th_err = abs(th_sim - th_obs) / max(1e-6, th_obs)
    if th_err < 0.05 and worst < 0.10:
        grade, verdict = "good", "Twin tracks the line within 5% on throughput."
    elif th_err < 0.15:
        grade, verdict = "fair", ("Twin is directionally right but absolute "
                                  "figures need calibration.")
    else:
        grade, verdict = "poor", ("Twin does not match the line. Calibrate before "
                                  "trusting any absolute number.")
    return {
        "throughput_sim": th_sim, "throughput_obs": th_obs,
        "throughput_err_pct": th_err * 100.0,
        "worst_stage_err_pct": worst * 100.0,
        "stages": rows, "grade": grade, "verdict": verdict,
        "reps": len(seeds),
    }


# --------------------------------------------------------------------------
# Fitting
# --------------------------------------------------------------------------
def calibrate(plant: Plant, observation: dict, seeds: List[int],
              max_iter: int = 8, damping: float = 0.6,
              tol: float = 0.03) -> dict:
    """Fit per-stage processing time to the observed shift.

    Returns the fitted plant, the error trajectory, and the patch list that
    takes the original plant to the fitted one -- so calibration is expressible
    in exactly the same language as a what-if scenario.
    """
    fitted = copy.deepcopy(plant)
    trajectory = [fidelity(fitted, observation, seeds)]
    # snapshot of the parameter vector that produced each trajectory entry,
    # so we can restore the best iterate exactly rather than re-deriving it
    snapshots = [[st.proc.mean for st in fitted.stages]]

    for _ in range(max_iter):
        last = trajectory[-1]
        if (last["worst_stage_err_pct"] / 100.0 < tol and
                last["throughput_err_pct"] / 100.0 < tol):
            break
        for row in last["stages"]:
            if row["busy_sim"] <= 1e-6:
                continue
            ratio = row["busy_obs"] / row["busy_sim"]
            adj = 1.0 + damping * (ratio - 1.0)
            adj = min(2.0, max(0.5, adj))      # never move more than 2x per step
            st = fitted.stages[fitted.index(row["id"])]
            st.proc.mean = max(0.5, st.proc.mean * adj)

        # Step 2 -- scale. Utilisation is degenerate under uniform time scaling
        # (see module docstring), so throughput is the only signal that can fix
        # the absolute scale. Without this step the fit converges to the right
        # ratios and the wrong magnitudes.
        th_sim = last["throughput_sim"]
        th_obs = last["throughput_obs"]
        if th_obs > 1e-9 and th_sim > 1e-9:
            g = 1.0 + damping * (th_sim / th_obs - 1.0)
            g = min(1.5, max(0.67, g))
            for st in fitted.stages:
                st.proc.mean = max(0.5, st.proc.mean * g)

        trajectory.append(fidelity(fitted, observation, seeds))
        snapshots.append([st.proc.mean for st in fitted.stages])

    # keep the best iterate, not merely the last -- damped iteration can
    # overshoot, and shipping a worse fit than we started with would be absurd
    def _score(t):
        # throughput is weighted more heavily: it is the number a plant manager
        # checks the twin against, and it is the term that pins the time scale
        return 2.0 * t["throughput_err_pct"] + t["worst_stage_err_pct"]

    best_i = min(range(len(trajectory)), key=lambda i: _score(trajectory[i]))
    for st, mean in zip(fitted.stages, snapshots[best_i]):
        st.proc.mean = mean

    patches = []
    for a, b in zip(plant.stages, fitted.stages):
        if abs(a.proc.mean - b.proc.mean) > 1e-6:
            patches.append({"stage": a.id, "field": "proc.mean", "op": "set",
                            "value": round(b.proc.mean, 3),
                            "was": round(a.proc.mean, 3)})

    return {
        "fitted_plant": fitted,
        "patches": patches,
        "before": trajectory[0],
        "after": trajectory[best_i],
        "iterations": len(trajectory) - 1,
        "trajectory": [{"iter": i,
                        "throughput_err_pct": t["throughput_err_pct"],
                        "worst_stage_err_pct": t["worst_stage_err_pct"]}
                       for i, t in enumerate(trajectory)],
        "method": ("Damped fixed-point on per-stage processing time. "
                   "Utilisation sets the ratios between stages; throughput "
                   "sets the absolute scale, because utilisation is degenerate "
                   "under uniform time scaling. Damping "
                   f"{damping}, tolerance {tol*100:.0f}%."),
    }
