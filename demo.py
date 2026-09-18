#!/usr/bin/env python3
"""
Standalone demonstration of the digital-twin simulation core.

    python3 demo.py                     # full run on the reference line
    python3 demo.py --plant x.json      # your own line
    python3 demo.py --reps 12           # more replications, tighter CIs
    python3 demo.py --json out.json     # dump machine-readable results

No third-party dependencies. Everything printed is computed, not hardcoded.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from dtwin import Plant, Simulation
from dtwin.analysis import full_report
from dtwin.report import render_baseline, render_comparison, render_ranking
from dtwin.whatif import Patch, compare, rank_interventions, replicate

HERE = os.path.dirname(os.path.abspath(__file__))

SCENARIOS = [
    ("Degradation: CNC cycle time +20% (tool wear)",
     [Patch("S3", "proc.mean", "mul", 1.20)]),
    ("Investment: +1 CNC machine at the constraint",
     [Patch("S3", "machines", "add", 1)]),
    ("Maintenance: halve CNC repair time (MTTR 600s -> 300s)",
     [Patch("S3", "repair.mean", "mul", 0.5)]),
    ("Decoupling: +10 buffer slots in front of CNC",
     [Patch("S3", "in_buffer", "add", 10)]),
    ("Non-constraint tinkering: packaging 30% faster",
     [Patch("S6", "proc.mean", "mul", 1 / 1.30)]),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plant", default=os.path.join(HERE, "plants", "mdfs.json"))
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--seed0", type=int, default=1)
    ap.add_argument("--json", default=None)
    ap.add_argument("--no-sweep", action="store_true")
    args = ap.parse_args()

    plant = Plant.load(args.plant)
    errs = plant.validate()
    if errs:
        print("invalid plant:", errs)
        return 1
    seeds = list(range(args.seed0, args.seed0 + args.reps))

    t0 = time.time()
    base_runs = replicate(plant, seeds)
    ths = [r["throughput_per_h"] for r in base_runs]
    mean = sum(ths) / len(ths)
    var = sum((x - mean) ** 2 for x in ths) / (len(ths) - 1)
    # representative run = the replication closest to the mean, so the detailed
    # state breakdown is typical rather than cherry-picked
    rep_seed = seeds[min(range(len(ths)), key=lambda i: abs(ths[i] - mean))]
    sim = Simulation(plant, seed=rep_seed).run()
    print(render_baseline(sim, th_override=mean, reps=len(seeds)))
    print(f"\n  across {len(seeds)} replications: throughput "
          f"{mean:.2f} +/- {1.96*(var**0.5)/len(seeds)**0.5:.2f} units/h (95% CI), "
          f"run-to-run SD {var**0.5:.2f}")
    print("  ^ that SD is why a single before/after re-run cannot be trusted.")

    fr = full_report(sim, th_override=mean)
    results = {"baseline": {k: v for k, v in fr.items() if k != "kpis"}}
    results["baseline_kpis"] = fr["kpis"]
    results["baseline_kpis"]["throughput_per_h_mean"] = mean
    results["scenarios"] = []

    for title, patches in SCENARIOS:
        cmp = compare(plant, patches, seeds, base_runs=base_runs)
        print(render_comparison(cmp, title))
        results["scenarios"].append({
            "title": title,
            "patches": cmp["patches"],
            "deltas": cmp["deltas"],
            "bottleneck_before": cmp["bottleneck_before"],
            "bottleneck_after": cmp["bottleneck_after"],
        })

    if not args.no_sweep:
        ranked = rank_interventions(plant, seeds, base_runs=base_runs)
        print(render_ranking(ranked))
        results["ranked_interventions"] = ranked

    print(f"\n[{time.time()-t0:.1f}s total, "
          f"{(len(SCENARIOS)+1+ (0 if args.no_sweep else 4*len(plant.stages)))*len(seeds)} "
          f"replications of {plant.horizon/3600:.0f}h each]")

    if args.json:
        def enc(o):
            try:
                json.dumps(o)
                return o
            except TypeError:
                return str(o)
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=2, default=enc)
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
