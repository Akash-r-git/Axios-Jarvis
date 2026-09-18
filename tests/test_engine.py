"""
Engine tests.

These are known-answer tests: each one checks the simulation against a result
that can be derived by hand, so a passing suite is evidence the engine is
correct rather than merely consistent.

Run standalone with `python3 -m tests.test_engine`, or via `python3 run.py
--check`. Works under pytest too, but does not require it -- the project has no
third-party dependencies and the tests should not introduce one.
"""
from __future__ import annotations

import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dtwin.analysis import capacity_table, full_report, kpis          # noqa: E402
from dtwin.engine import BLOCKED, BUSY, DOWN, STARVED, Simulation     # noqa: E402
from dtwin.model import Dist, Plant, Stage                            # noqa: E402
from dtwin.narrate import verify_numbers                              # noqa: E402
from dtwin.whatif import Patch, apply_patches, compare, replicate     # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# --------------------------------------------------------------------------
# tiny harness
# --------------------------------------------------------------------------
_RESULTS = []


def check(name):
    def deco(fn):
        _RESULTS.append((name, fn))
        return fn
    return deco


def approx(got, want, tol, label=""):
    if abs(got - want) > tol:
        raise AssertionError(f"{label or 'value'}: got {got:.4f}, "
                             f"expected {want:.4f} ± {tol}")


def line(*stages, horizon=7200.0, warmup=600.0) -> Plant:
    return Plant(name="test", stages=list(stages), horizon=horizon, warmup=warmup)


def stage(sid, t, machines=1, buf=-1, cv=0.0, mtbf=None, mttr=0.0, yld=1.0,
          setup=0.0, batch=0) -> Stage:
    from dtwin.model import INF
    return Stage(id=sid, name=sid, machines=machines,
                 proc=Dist("lognorm" if cv else "det", t, cv),
                 in_buffer=INF if buf < 0 else float(buf),
                 mtbf=INF if not mtbf else float(mtbf),
                 repair=Dist("det", mttr, 0.0), yield_rate=yld,
                 setup_time=setup, batch_size=batch)


# ==========================================================================
# 1. Deterministic single stage -> exact throughput
# ==========================================================================
@check("Single deterministic stage produces exactly 60 units/h")
def t_single():
    p = line(stage("S1", 60.0), horizon=7200, warmup=600)
    k = kpis(Simulation(p, seed=1).run())
    approx(k["throughput_per_h"], 60.0, 0.6, "throughput")
    approx(k["stages"][0]["busy"], 1.0, 0.001, "busy fraction")


# ==========================================================================
# 2. Blocking-after-service: a starved-for-space upstream stage
# ==========================================================================
@check("Upstream blocked fraction converges to 50% when downstream is 2x slower")
def t_blocking():
    p = line(stage("S1", 30.0), stage("S2", 60.0, buf=1), horizon=14400, warmup=1200)
    k = kpis(Simulation(p, seed=1).run())
    up = k["stages"][0]
    # S1 can do two units in the time S2 does one, so half its time is spent
    # holding finished work it cannot hand over
    if not 0.45 <= up["blocked"] <= 0.55:
        raise AssertionError(f"S1 blocked fraction {up['blocked']:.3f}, expected ~0.50")
    approx(k["throughput_per_h"], 60.0, 1.0, "throughput (set by slow stage)")


# ==========================================================================
# 3. Starvation is the mirror image
# ==========================================================================
@check("Downstream waiting fraction converges to 50% when upstream is 2x slower")
def t_starvation():
    p = line(stage("S1", 60.0), stage("S2", 30.0, buf=2), horizon=14400, warmup=1200)
    k = kpis(Simulation(p, seed=1).run())
    dn = k["stages"][1]
    if not 0.45 <= dn["starved"] <= 0.55:
        raise AssertionError(f"S2 waiting fraction {dn['starved']:.3f}, expected ~0.50")


# ==========================================================================
# 4. Availability A = MTBF/(MTBF+MTTR)
# ==========================================================================
@check("Simulated downtime fraction matches MTBF/(MTBF+MTTR)")
def t_availability():
    # operation-dependent failures: expected down fraction of total machine time
    # is MTTR/(MTBF+MTTR) only when the machine is never starved or blocked,
    # which holds for a saturated single stage
    p = line(stage("S1", 30.0, mtbf=1000.0, mttr=250.0), horizon=200000, warmup=20000)
    downs = [kpis(Simulation(p, seed=s).run())["stages"][0]["down"] for s in range(1, 13)]
    expected = 250.0 / (1000.0 + 250.0)
    approx(statistics.mean(downs), expected, 0.03, "mean down fraction")


# ==========================================================================
# 5. Yield
# ==========================================================================
@check("Scrap rate matches the configured yield")
def t_yield():
    p = line(stage("S1", 20.0, yld=0.90), horizon=200000, warmup=20000)
    ks = [kpis(Simulation(p, seed=s).run()) for s in range(1, 9)]
    approx(statistics.mean(k["first_pass_yield"] for k in ks), 0.90, 0.02, "first-pass yield")


# ==========================================================================
# 6. Changeover cost
# ==========================================================================
@check("Changeover reduces capacity by exactly setup/batch per unit")
def t_setup():
    p = line(stage("S1", 40.0, setup=200.0, batch=10), horizon=200000, warmup=20000)
    k = kpis(Simulation(p, seed=1).run())
    expected = 3600.0 / (40.0 + 200.0 / 10.0)      # 60 units/h
    approx(k["throughput_per_h"], expected, 1.5, "throughput with changeover")


# ==========================================================================
# 7. Determinism
# ==========================================================================
@check("Same plant and seed give bit-identical results")
def t_determinism():
    p = Plant.load(os.path.join(ROOT, "plants", "mdfs.json"))
    a = kpis(Simulation(p, seed=7).run())
    b = kpis(Simulation(p, seed=7).run())
    for key in ("throughput_per_h", "wip", "cycle_time_s", "good", "scrap_total"):
        if a[key] != b[key]:
            raise AssertionError(f"{key} differs between identical runs: {a[key]} vs {b[key]}")


# ==========================================================================
# 8. Common random numbers isolate the edited stage
# ==========================================================================
@check("Editing one stage does not disturb another stage's random draws")
def t_crn():
    p = Plant.load(os.path.join(ROOT, "plants", "mdfs.json"))
    q = apply_patches(p, [Patch("S6", "proc.mean", "mul", 1.5)])
    a = Simulation(p, seed=11).run()
    b = Simulation(q, seed=11).run()
    # S1 is upstream of the edit and unconstrained by it at this magnitude;
    # its per-unit processing draws come from its own stream, so its total
    # busy time per completed unit must be essentially unchanged
    ka, kb = kpis(a), kpis(b)
    ra = ka["stages"][0]["busy"] / max(ka["throughput_per_h"], 1e-9)
    rb = kb["stages"][0]["busy"] / max(kb["throughput_per_h"], 1e-9)
    if abs(ra - rb) / ra > 0.05:
        raise AssertionError(f"S1 busy-per-unit moved {abs(ra-rb)/ra*100:.1f}% "
                             f"after editing S6; RNG streams are leaking")


# ==========================================================================
# 9. Self-validation checks all pass on every shipped line
# ==========================================================================
@check("All internal consistency checks pass on every shipped line")
def t_validation():
    for fn in sorted(os.listdir(os.path.join(ROOT, "plants"))):
        if not fn.endswith(".json"):
            continue
        p = Plant.load(os.path.join(ROOT, "plants", fn))
        runs = replicate(p, list(range(1, 7)))
        mean = statistics.mean(r["throughput_per_h"] for r in runs)
        rep = Simulation(p, seed=1).run()
        rpt = full_report(rep, th_override=mean)
        for c in rpt["validation"]:
            if not c["pass"]:
                raise AssertionError(f"{fn}: check failed -- {c['name']} ({c['value']})")


# ==========================================================================
# 10. Throughput never exceeds the analytical capacity bound
# ==========================================================================
@check("Replication-mean throughput stays under the closed-form capacity bound")
def t_capacity_bound():
    for fn in ("mdfs.json", "smt_line.json", "pharma_packaging.json"):
        p = Plant.load(os.path.join(ROOT, "plants", fn))
        bound = min(c["r_good"] for c in capacity_table(p))
        runs = replicate(p, list(range(1, 9)))
        mean = statistics.mean(r["throughput_per_h"] for r in runs)
        if mean > bound * 1.02:
            raise AssertionError(f"{fn}: throughput {mean:.2f} exceeds bound {bound:.2f}")


# ==========================================================================
# 11. What-if at a non-constraint is correctly reported as no effect
# ==========================================================================
@check("Speeding up a non-constraint returns a statistically null result")
def t_null_result():
    p = Plant.load(os.path.join(ROOT, "plants", "mdfs.json"))
    seeds = list(range(1, 11))
    cmp = compare(p, [Patch("S6", "proc.mean", "mul", 1 / 1.3)], seeds)
    th = cmp["deltas"]["throughput_per_h"]
    if th["significant"] and th["delta"] > 1.0:
        raise AssertionError(f"non-constraint change credited with "
                             f"{th['delta']:+.2f} units/h; that should be noise")


# ==========================================================================
# 12. What-if at the constraint is correctly reported as a real gain
# ==========================================================================
@check("Adding a machine at the constraint gives a significant gain and moves it")
def t_real_result():
    p = Plant.load(os.path.join(ROOT, "plants", "mdfs.json"))
    seeds = list(range(1, 11))
    cmp = compare(p, [Patch("S3", "machines", "add", 1)], seeds)
    th = cmp["deltas"]["throughput_per_h"]
    if not th["significant"] or th["delta"] < 5.0:
        raise AssertionError(f"constraint investment showed {th['delta']:+.2f} units/h "
                             f"(significant={th['significant']}); expected a clear gain")
    if not cmp["bottleneck_migrated"]:
        raise AssertionError("bottleneck should migrate once the constraint is relieved")


# ==========================================================================
# 13. Idle attribution is complete
# ==========================================================================
@check("Every idle second is attributed to a root cause")
def t_attribution():
    p = Plant.load(os.path.join(ROOT, "plants", "mdfs.json"))
    sim = Simulation(p, seed=5).run()
    idle = sum(sim.state_time[i][STARVED] + sim.state_time[i][BLOCKED]
               for i in range(len(p.stages)))
    attributed = sum(sim.attrib.values())
    if idle > 0 and abs(idle - attributed) / idle > 1e-9:
        raise AssertionError(f"attribution gap: {attributed:.2f}s of {idle:.2f}s")


# ==========================================================================
# 14. Frame recording covers the shift
# ==========================================================================
@check("Playback frames span the whole shift with correct stride")
def t_frames():
    p = Plant.load(os.path.join(ROOT, "plants", "mdfs.json"))
    sim = Simulation(p, seed=2, frame_interval=30.0).run()
    if not sim.frames:
        raise AssertionError("no frames recorded")
    if sim.frames[0]["t"] != 0.0:
        raise AssertionError("frames must start at t=0")
    if sim.frames[-1]["t"] < p.horizon - 60:
        raise AssertionError(f"frames stop at {sim.frames[-1]['t']}, "
                             f"horizon is {p.horizon}")
    # every frame must account for every physical machine exactly once
    for f in sim.frames:
        for j, st in enumerate(f["st"]):
            if sum(st["s"]) != p.stages[j].machines:
                raise AssertionError(
                    f"frame at t={f['t']} shows {sum(st['s'])} machines at "
                    f"{p.stages[j].id}, line has {p.stages[j].machines}")


# ==========================================================================
# 15. Narration guard rejects invented figures
# ==========================================================================
@check("Narration guard rejects numbers that are not in the data")
def t_narration_guard():
    data = {"throughput": 92.11, "amplification": 3.34}
    if verify_numbers("Throughput was 92.1 with amplification 3.34.", data):
        raise AssertionError("guard rejected figures that are present in the data")
    bad = verify_numbers("Throughput was 4711.5 units.", data)
    if "4711.5" not in bad:
        raise AssertionError("guard failed to reject an invented figure")


# ==========================================================================
# 16. Calibration recovers known parameters
# ==========================================================================
@check("Calibration recovers hidden cycle times from one observed shift")
def t_calibration():
    import copy
    from dtwin.calibrate import calibrate, load_observation
    obs_path = os.path.join(ROOT, "data", "observed_shift.csv")
    if not os.path.exists(obs_path):
        raise AssertionError("data/observed_shift.csv is missing")
    base = Plant.load(os.path.join(ROOT, "plants", "mdfs.json"))
    truth = copy.deepcopy(base)
    for sid, val in (("S1", 46.5), ("S3", 61.0), ("S5", 21.5)):
        truth.stages[truth.index(sid)].proc.mean = val

    obs = load_observation(obs_path)
    fit = calibrate(base, obs, list(range(1, 7)), max_iter=10)
    if fit["after"]["throughput_err_pct"] > 4.0:
        raise AssertionError(f"calibrated throughput error "
                             f"{fit['after']['throughput_err_pct']:.1f}%, expected under 4%")
    errs = []
    for p in fit["patches"]:
        tv = truth.stages[truth.index(p["stage"])].proc.mean
        errs.append(abs(p["value"] - tv) / tv)
    if errs and statistics.mean(errs) > 0.05:
        raise AssertionError(f"mean parameter recovery error "
                             f"{statistics.mean(errs)*100:.1f}%, expected under 5%")


# ==========================================================================
def run_all() -> bool:
    print("\n  Maestro Twin — engine tests\n" + "  " + "-" * 66)
    passed = failed = 0
    for name, fn in _RESULTS:
        try:
            fn()
            print(f"  \033[32mPASS\033[0m  {name}")
            passed += 1
        except Exception as exc:
            print(f"  \033[31mFAIL\033[0m  {name}\n        {exc}")
            failed += 1
    print("  " + "-" * 66)
    print(f"  {passed} passed, {failed} failed\n")
    return failed == 0


# pytest compatibility without requiring it
def _make_pytest_wrappers():
    g = globals()
    for i, (name, fn) in enumerate(_RESULTS):
        g[f"test_{i:02d}_{fn.__name__.lstrip('t_')}"] = fn


_make_pytest_wrappers()

if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
