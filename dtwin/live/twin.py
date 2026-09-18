"""
The live twin.

Holds canonical per-machine state for a line, a bounded event ledger and
counters. It is updated one event at a time and every update is O(1) in the
number of events: no simulation is ever run from here. Simulation and what-if
work on a *separate* Plant object (see dtwin.whatif), which is why this module
imports nothing from the engine beyond the data model.

The live twin is authoritative about what was OBSERVED. Anything it deduces
(e.g. "OP10 is probably blocked because OP20 is down") is tagged INFERRED and
kept separate from the observed record.
"""
from __future__ import annotations

import copy
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from dtwin.model import INF, Plant
from dtwin.live.events import (BLOCKED, BUSY, DOWN, IDLE, INFERRED, OBSERVED,
                               SETUP, STARVED, BUFFER, COUNTER, CYCLE,
                               DOWNTIME, HEARTBEAT, STATE_CHANGE, Event)

DEFAULT_LEDGER = 2000


class MachineState:
    __slots__ = ("stage", "machine", "state", "since", "provenance", "source",
                 "durations", "last_ts")

    def __init__(self, stage: str, machine: str, ts: float):
        self.stage = stage
        self.machine = machine
        self.state: Optional[str] = None
        self.since = ts
        self.provenance = OBSERVED
        self.source = ""
        self.durations: Dict[str, float] = {}
        self.last_ts = ts

    def to_dict(self, now: float) -> dict:
        return {
            "stage": self.stage, "machine": self.machine,
            "state": self.state, "since": self.since,
            "for_s": max(0.0, now - self.since),
            "provenance": self.provenance, "source": self.source,
            "durations": dict(self.durations),
        }


class LiveTwin:
    """Canonical live state for one line.

    `plant` supplies topology only (stage order, buffer capacities). Its
    parameters are never modified by live events: parameter estimation is a
    separate, explicit step (dtwin.ingest.estimate).
    """

    def __init__(self, plant: Plant, ledger_size: int = DEFAULT_LEDGER,
                 name: Optional[str] = None):
        self.plant = plant
        self.name = name or plant.name
        self.stage_order: List[str] = [s.id for s in plant.stages]
        self.stage_names: Dict[str, str] = {s.id: s.name for s in plant.stages}
        self.machines: Dict[Tuple[str, str], MachineState] = {}
        self.ledger: deque = deque(maxlen=ledger_size)
        self.rejected: deque = deque(maxlen=200)
        self.ledger_size = ledger_size
        self.buffers: Dict[str, dict] = {}
        self.counters: Dict[str, float] = {
            "events_applied": 0, "events_rejected": 0,
            "good": 0, "scrap": 0, "failures": 0,
        }
        self.cycles: Dict[str, deque] = {}
        self.downtimes: Dict[str, deque] = {}
        self.started = time.time()
        self.last_event_ts: Optional[float] = None
        self.lock = threading.Lock()
        self._listeners: List[Any] = []
        # seed declared machines so the UI has a full grid before any traffic
        for s in plant.stages:
            for m in range(s.machines):
                mid = f"{s.id}-M{m + 1}" if s.machines > 1 else s.id
                self.machines[(s.id, mid)] = MachineState(s.id, mid,
                                                          self.started)
            if s.in_buffer != INF:
                self.buffers[s.id] = {"stage": s.id, "level": None,
                                      "capacity": s.in_buffer,
                                      "provenance": None, "ts": None}

    # ------------------------------------------------------------------
    # applying events
    # ------------------------------------------------------------------
    def apply(self, ev: Event) -> Tuple[bool, List[str]]:
        """Apply one event. Returns (applied, problems).

        A malformed event is recorded in the reject log and does NOT change
        state — the last known good state is preserved.
        """
        problems = ev.problems()
        if not problems and ev.stage not in self.stage_order:
            problems = [f"unknown stage {ev.stage!r}"]
        if problems:
            with self.lock:
                self.counters["events_rejected"] += 1
                self.rejected.append({"event": ev.to_dict(),
                                      "problems": problems,
                                      "at": time.time()})
            return False, problems

        with self.lock:
            key = (ev.stage, ev.machine)
            ms = self.machines.get(key)
            if ms is None:
                ms = MachineState(ev.stage, ev.machine, ev.ts)
                self.machines[key] = ms

            if ev.event_type == STATE_CHANGE:
                if ms.state is not None and ev.ts >= ms.since:
                    ms.durations[ms.state] = ms.durations.get(ms.state, 0.0) \
                        + (ev.ts - ms.since)
                if ms.state != ev.new_state:
                    ms.since = ev.ts
                if ev.new_state == DOWN and ms.state != DOWN:
                    self.counters["failures"] += 1
                ms.state = ev.new_state
                ms.provenance = ev.provenance
                ms.source = ev.source
            elif ev.event_type == COUNTER:
                kind = "scrap" if str(ev.raw or {}).find("scrap") >= 0 else None
                kind = kind or ("scrap" if (ev.raw or {}).get("kind") == "scrap"
                                else "good")
                self.counters[kind] += float(ev.value)
            elif ev.event_type == BUFFER:
                cap = self.buffers.get(ev.stage, {}).get("capacity")
                self.buffers[ev.stage] = {
                    "stage": ev.stage, "level": float(ev.value),
                    "capacity": cap, "provenance": ev.provenance, "ts": ev.ts}
            elif ev.event_type == CYCLE:
                self.cycles.setdefault(ev.stage, deque(maxlen=500)).append(
                    float(ev.value))
            elif ev.event_type == DOWNTIME:
                self.downtimes.setdefault(ev.stage, deque(maxlen=300)).append(
                    float(ev.value))

            ms.last_ts = ev.ts
            self.counters["events_applied"] += 1
            self.last_event_ts = ev.ts
            if ev.event_type != HEARTBEAT:
                self.ledger.append(ev.to_dict())
        self._notify(ev)
        return True, []

    def apply_many(self, events: List[Event]) -> Tuple[int, int]:
        ok = bad = 0
        for ev in events:
            applied, _ = self.apply(ev)
            ok += 1 if applied else 0
            bad += 0 if applied else 1
        return ok, bad

    # ------------------------------------------------------------------
    # listeners (server-sent events)
    # ------------------------------------------------------------------
    def subscribe(self, q) -> None:
        self._listeners.append(q)

    def unsubscribe(self, q) -> None:
        try:
            self._listeners.remove(q)
        except ValueError:
            pass

    def _notify(self, ev: Event) -> None:
        for q in list(self._listeners):
            try:
                q.put_nowait(ev.to_dict())
            except Exception:
                pass          # a slow consumer must never stall ingestion

    # ------------------------------------------------------------------
    # derived views
    # ------------------------------------------------------------------
    def stage_states(self) -> List[dict]:
        now = time.time()
        out = []
        for sid in self.stage_order:
            mss = [m for (s, _), m in self.machines.items() if s == sid]
            states = [m.state for m in mss if m.state]
            out.append({
                "stage": sid, "name": self.stage_names.get(sid, sid),
                "machines": [m.to_dict(now) for m in sorted(
                    mss, key=lambda m: m.machine)],
                "down": sum(1 for s in states if s == DOWN),
                "running": sum(1 for s in states if s == BUSY),
                "buffer": self.buffers.get(sid),
            })
        return out

    def propagation(self) -> List[dict]:
        """Downtime propagation, split into what was seen and what is deduced.

        For every stage with a machine DOWN, the upstream stage is expected to
        go BLOCKED (its output has nowhere to go once the buffer fills) and the
        downstream stage to go STARVED. If the source actually reported those
        states we mark them OBSERVED; otherwise the effect is the twin's own
        deduction from serial topology and is marked INFERRED.
        """
        by_stage = {sid: [] for sid in self.stage_order}
        for (sid, _), ms in self.machines.items():
            if sid in by_stage:
                by_stage[sid].append(ms)

        chains: List[dict] = []
        for i, sid in enumerate(self.stage_order):
            downs = [m for m in by_stage[sid] if m.state == DOWN]
            if not downs:
                continue
            all_down = len(downs) == len(by_stage[sid]) and by_stage[sid]
            effects = []
            if i > 0:
                up = self.stage_order[i - 1]
                seen = [m for m in by_stage[up] if m.state == BLOCKED]
                effects.append({
                    "stage": up, "name": self.stage_names.get(up, up),
                    "effect": BLOCKED,
                    "provenance": OBSERVED if seen else INFERRED,
                    "machines": [m.machine for m in seen],
                    "note": ("reported by the source" if seen else
                             "expected once the feeding buffer fills; not yet "
                             "reported"),
                })
            if i < len(self.stage_order) - 1:
                dn = self.stage_order[i + 1]
                seen = [m for m in by_stage[dn] if m.state == STARVED]
                effects.append({
                    "stage": dn, "name": self.stage_names.get(dn, dn),
                    "effect": STARVED,
                    "provenance": OBSERVED if seen else INFERRED,
                    "machines": [m.machine for m in seen],
                    "note": ("reported by the source" if seen else
                             "expected once the feeding buffer drains; not yet "
                             "reported"),
                })
            chains.append({
                "cause_stage": sid,
                "cause_name": self.stage_names.get(sid, sid),
                "cause_machines": [m.machine for m in downs],
                "cause_provenance": OBSERVED,
                "stage_fully_down": bool(all_down),
                "down_for_s": max((time.time() - m.since) for m in downs),
                "effects": effects,
            })
        return chains

    def summary_line(self) -> str:
        """One-line propagation readout, e.g.
        'OP20 DOWN -> OP10 BLOCKED (observed) -> OP30 STARVED (inferred)'."""
        chains = self.propagation()
        if not chains:
            return "No stage is down."
        c = chains[0]
        bits = [f"{c['cause_stage']} DOWN"]
        for e in c["effects"]:
            bits.append(f"{e['stage']} {e['effect']} ({e['provenance']})")
        return " -> ".join(bits)

    def snapshot(self, events: int = 60) -> dict:
        return {
            "name": self.name,
            "stages": self.stage_states(),
            "propagation": self.propagation(),
            "summary": self.summary_line(),
            "counters": dict(self.counters),
            "ledger_size": len(self.ledger),
            "ledger_cap": self.ledger_size,
            "events": list(self.ledger)[-events:],
            "rejected": list(self.rejected)[-10:],
            "last_event_ts": self.last_event_ts,
            "cycle_samples": {k: len(v) for k, v in self.cycles.items()},
            "downtime_samples": {k: len(v) for k, v in self.downtimes.items()},
        }

    # ------------------------------------------------------------------
    # handing work to the simulation side
    # ------------------------------------------------------------------
    def plant_copy(self) -> Plant:
        """A deep copy of the topology for simulation / what-if.

        Every path from the live twin into the engine goes through here, so a
        what-if can never mutate live state.
        """
        return copy.deepcopy(self.plant)
