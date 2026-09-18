"""Tiny test harness shared by the intake-layer suites.

Same shape as the one inside tests/test_engine.py (which is left untouched so
the original 31 tests keep running exactly as before).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Suite:
    def __init__(self, title: str):
        self.title = title
        self.cases = []

    def check(self, name):
        def deco(fn):
            self.cases.append((name, fn))
            return fn
        return deco

    def run(self) -> tuple:
        print(f"\n  Maestro Twin — {self.title}\n  " + "-" * 66)
        passed = failed = 0
        for name, fn in self.cases:
            try:
                fn()
                print(f"  \033[32mPASS\033[0m  {name}")
                passed += 1
            except Exception as exc:
                print(f"  \033[31mFAIL\033[0m  {name}\n        "
                      f"{type(exc).__name__}: {exc}")
                failed += 1
        print("  " + "-" * 66)
        print(f"  {passed} passed, {failed} failed\n")
        return passed, failed

    def export_pytest(self, g: dict) -> None:
        for i, (_, fn) in enumerate(self.cases):
            g[f"test_{i:02d}_{fn.__name__.lstrip('t_')}"] = fn


def approx(got, want, tol, label=""):
    if abs(got - want) > tol:
        raise AssertionError(f"{label or 'value'}: got {got}, expected "
                             f"{want} ± {tol}")


def assert_true(cond, msg):
    if not cond:
        raise AssertionError(msg)
