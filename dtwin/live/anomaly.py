"""
Baseline-deviation anomaly detection on the live event stream.

Cheap and O(1) per event: every check is a comparison against a per-stage
baseline taken from the Plant, or against a small bounded window. No simulation
is run here, ever.

An anomaly says what, when, where, what was observed, what was expected, and how
severe it is. Nothing is scored by a model; the thresholds are explicit
constants and are reported alongside the finding.
"""
from __future__ import annotations

import statistics
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from dtwin.model import INF, Plant
from dtwin.live.events import (BUFFER, BUSY, COUNTER, CYCLE, DOWN, DOWNTIME,
                               STATE_CHANGE, Event)

# thresholds (explicit, reported with every finding)
DOWNTIME_FACTOR = 3.0        # x baseline MTTR before a stop is abnormal
CYCLE_FACTOR = 1.5           # x baseline cycle time
CYCLE_MIN_SAMPLES = 8
FAILURE_WINDOW_S = 900.0
FAILURE_COUNT = 4
QUEUE_FILL = 0.9
THROUGHPUT_DROP = 0.6        # fraction of expected rate
THROUGHPUT_MIN_S = 900.0
COOLDOWN_S = 300.0           # per (kind, stage), in event time


class AnomalyDetector:
    def __init__(self, plant: Plant):
        self.baseline: Dict[str, dict] = {}
        for s in plant.stages:
            self.baseline[s.id] = {
                "cycle_s": s.proc.mean,
                "mttr_s": (s.repair.mean if s.repair.mean > 0 else None),
                "mtbf_s": (None if s.mtbf == INF else s.mtbf),
                "buffer_cap": (None if s.in_buffer == INF else s.in_buffer),
                "capacity_per_h": s.capacity_per_h(),
            }
        self.line_capacity_per_h = min(
            (b["capacity_per_h"] for b in self.baseline.values()), default=None)
        self.cycles: Dict[str, Deque[float]] = {}
        self.failures: Dict[str, Deque[float]] = {}
        self.down_since: Dict[tuple, float] = {}
        self.last_fired: Dict[tuple, float] = {}
        self.first_ts: Optional[float] = None
        self.good = 0.0

    # ------------------------------------------------------------------
    def _fire(self, kind: str, ev: Event, observed: Any, expected: Any,
              message: str, severity: str, confidence: float = 0.8,
              rule: str = "") -> Optional[dict]:
        key = (kind, ev.stage)
        last = self.last_fired.get(key)
        if last is not None and ev.ts - last < COOLDOWN_S:
            return None
        self.last_fired[key] = ev.ts
        return {"kind": kind, "stage": ev.stage, "machine": ev.machine,
                "ts": ev.ts, "observed": observed, "expected": expected,
                "severity": severity, "confidence": confidence,
                "message": message, "rule": rule,
                "provenance": ev.provenance}

    # ------------------------------------------------------------------
    def observe(self, ev: Event, twin) -> List[dict]:
        """Inspect one applied event. Returns zero or more anomalies."""
        out: List[dict] = []
        base = self.baseline.get(ev.stage)
        if base is None:
            return out
        if self.first_ts is None:
            self.first_ts = ev.ts

        # ---- explicit downtime record --------------------------------
        if ev.event_type == DOWNTIME and isinstance(ev.value, (int, float)):
            out += self._check_stop(ev, base, float(ev.value))

        # ---- state transitions ---------------------------------------
        if ev.event_type == STATE_CHANGE:
            key = (ev.stage, ev.machine)
            if ev.new_state == DOWN:
                self.down_since[key] = ev.ts
                w = self.failures.setdefault(ev.stage, deque(maxlen=64))
                w.append(ev.ts)
                while w and ev.ts - w[0] > FAILURE_WINDOW_S:
                    w.popleft()
                if len(w) >= FAILURE_COUNT:
                    a = self._fire(
                        "repeat_failures", ev, f"{len(w)} stops",
                        f"< {FAILURE_COUNT} stops per "
                        f"{int(FAILURE_WINDOW_S / 60)} min",
                        f"{ev.stage} has stopped {len(w)} times in the last "
                        f"{int(FAILURE_WINDOW_S / 60)} minutes.",
                        "high", 0.9,
                        f"{FAILURE_COUNT}+ DOWN transitions within "
                        f"{FAILURE_WINDOW_S:.0f}s")
                    if a:
                        out.append(a)
            elif key in self.down_since:
                dur = ev.ts - self.down_since.pop(key)
                out += self._check_stop(ev, base, dur)

        # ---- cycle time ----------------------------------------------
        if ev.event_type == CYCLE and isinstance(ev.value, (int, float)):
            w = self.cycles.setdefault(ev.stage, deque(maxlen=60))
            w.append(float(ev.value))
            expected = base["cycle_s"]
            if len(w) >= CYCLE_MIN_SAMPLES and expected > 0:
                mean = statistics.fmean(w)
                if mean > expected * CYCLE_FACTOR:
                    a = self._fire(
                        "cycle_deviation", ev, f"{mean:.1f}s",
                        f"{expected:.1f}s",
                        f"{ev.stage} is averaging {mean:.1f}s per part against "
                        f"a baseline of {expected:.1f}s over the last "
                        f"{len(w)} cycles.",
                        "high" if mean > expected * 2 else "medium", 0.85,
                        f"rolling mean > {CYCLE_FACTOR}x baseline over "
                        f"{CYCLE_MIN_SAMPLES}+ samples")
                    if a:
                        out.append(a)

        # ---- buffer build-up -----------------------------------------
        if ev.event_type == BUFFER and isinstance(ev.value, (int, float)):
            cap = base["buffer_cap"]
            if cap and cap > 0 and float(ev.value) >= cap * QUEUE_FILL:
                a = self._fire(
                    "queue_buildup", ev, f"{float(ev.value):.0f} parts",
                    f"< {cap * QUEUE_FILL:.0f} parts",
                    f"The buffer feeding {ev.stage} is at "
                    f"{float(ev.value):.0f}/{cap:.0f}; upstream will block "
                    f"when it fills.",
                    "medium", 0.8,
                    f"buffer level >= {int(QUEUE_FILL * 100)}% of capacity")
                if a:
                    out.append(a)

        # ---- throughput ----------------------------------------------
        if ev.event_type == COUNTER and isinstance(ev.value, (int, float)):
            self.good += float(ev.value)
            elapsed = ev.ts - (self.first_ts or ev.ts)
            cap = self.line_capacity_per_h
            if elapsed > THROUGHPUT_MIN_S and cap:
                rate = self.good / (elapsed / 3600.0)
                if rate < cap * THROUGHPUT_DROP:
                    a = self._fire(
                        "throughput_drop", ev, f"{rate:.1f}/h",
                        f"~{cap:.1f}/h",
                        f"Output is running at {rate:.1f} units/h against a "
                        f"line capacity of {cap:.1f} units/h.",
                        "high", 0.7,
                        f"observed rate < {int(THROUGHPUT_DROP * 100)}% of the "
                        f"baseline capacity")
                    if a:
                        out.append(a)
        return out

    # ------------------------------------------------------------------
    def _check_stop(self, ev: Event, base: dict, dur: float) -> List[dict]:
        expected = base["mttr_s"]
        if not expected or dur <= expected * DOWNTIME_FACTOR:
            return []
        a = self._fire(
            "downtime_duration", ev, f"{dur:.0f}s", f"~{expected:.0f}s (MTTR)",
            f"A stop on {ev.stage} lasted {dur / 60:.1f} min against a baseline "
            f"repair time of {expected / 60:.1f} min.",
            "high" if dur > expected * 5 else "medium", 0.85,
            f"stop duration > {DOWNTIME_FACTOR}x baseline MTTR")
        return [a] if a else []
