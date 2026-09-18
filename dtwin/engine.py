"""
Discrete-event simulation kernel for a serial multi-stage line with
parallel machines, finite buffers, blocking-after-service, operation-dependent
breakdowns, changeovers and scrap.

Design notes that matter:

* Event-driven, not time-stepped. The clock jumps to the next state change, so
  an 8-hour shift costs ~30k events instead of 28.8M ticks.
* Blocking-after-service (BAS): a machine that finishes a unit with no room
  downstream keeps holding it and stops working. This is what makes downtime
  propagate *upstream*; a model without it cannot answer the problem statement.
* Operation-dependent failures: the time-to-failure clock only runs while the
  machine is actually processing, and a failure preempts-and-resumes the
  current unit. Wall-clock failure models overstate availability on starved
  lines.
* Per-stage, per-purpose RNG streams. Changing a parameter at stage 3 does not
  shift the random numbers consumed at stage 1. This is what makes what-if
  deltas attributable to the change rather than to noise (common random
  numbers).
* Every idle second is attributed to a root cause elsewhere in the line by
  walking the empty/full buffer chain. That attribution matrix IS the
  "downtime propagation" deliverable.
"""
from __future__ import annotations

import heapq
import random
import zlib
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple

from .model import INF, Plant, Stage

# machine states
BUSY, SETUP, DOWN, BLOCKED, STARVED = "BUSY", "SETUP", "DOWN", "BLOCKED", "STARVED"
ACTIVE_STATES = (BUSY, SETUP, DOWN)      # "would be producing if it could"
IDLE_STATES = (BLOCKED, STARVED)         # "stopped because of someone else"

# event kinds
E_SERVICE_END, E_FAILURE, E_REPAIR, E_SETUP_END, E_ARRIVAL, E_RESET, E_END = range(7)

SOURCE = -1
SINK = -2


def _stream(seed: int, key: str, name: str) -> random.Random:
    """Deterministic, independent RNG stream. crc32 (not hash()) because
    Python's string hash is salted per process."""
    h = zlib.crc32(f"{seed}|{key}|{name}".encode()) & 0xFFFFFFFF
    return random.Random(h)


class Simulation:
    def __init__(self, plant: Plant, seed: int = 1, record_timeline: bool = False,
                 timeline_cap: int = 20000, frame_interval: float = 0.0):
        """frame_interval > 0 records a compact state snapshot every N simulated
        seconds. The UI replays those frames to animate the line, which keeps
        playback deterministic and scrubbable rather than depending on a live
        background thread."""
        errs = plant.validate()
        if errs:
            raise ValueError("invalid plant: " + "; ".join(errs))

        self.p = plant
        self.seed = seed
        self.n = len(plant.stages)
        self.horizon = plant.horizon
        self.warmup = plant.warmup
        self.t = 0.0
        self.evq: List[tuple] = []
        self._seq = 0

        self.rng: Dict[Tuple[str, str], random.Random] = {}
        for s in plant.stages:
            for nm in ("proc", "fail", "rep", "qual"):
                self.rng[(s.id, nm)] = _stream(seed, s.id, nm)
        self.rng_src = _stream(seed, "__source__", "arr")

        self.buf: List[deque] = [deque() for _ in range(self.n)]
        self.mach: List[List[dict]] = []
        for si, s in enumerate(plant.stages):
            self.mach.append([{
                "state": STARVED, "part": None, "remaining": 0.0, "svc_start": 0.0,
                "ttf": self._draw_ttf(s), "cause": (SOURCE, "SOURCE"),
                "n_since_setup": 0, "blocked_at": 0.0,
            } for _ in range(s.machines)])

        self.active_since: List[Optional[float]] = [None] * self.n
        self.record_timeline = record_timeline
        self.timeline_cap = timeline_cap
        self.timeline: List[tuple] = []
        self.frame_interval = frame_interval
        self.frames: List[dict] = []
        self._next_frame_t = 0.0
        self.part_seq = 0
        self._zero_stats()

    # ------------------------------------------------------------------
    # statistics containers
    # ------------------------------------------------------------------
    def _zero_stats(self):
        self.stats_start = self.t
        self.state_time = [defaultdict(float) for _ in range(self.n)]
        self.attrib: Dict[Tuple[int, str, int, str], float] = defaultdict(float)
        self.buf_area = [0.0] * self.n
        self.wip_area = 0.0
        self.bn_time = [0.0] * self.n
        self.bn_shift = 0.0
        self.good = 0
        self.scrap = [0] * self.n
        self.failures = [0] * self.n
        self.released = 0
        self.cts: List[float] = []

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _draw_ttf(self, s: Stage) -> float:
        if s.mtbf == INF or s.mtbf <= 0:
            return INF
        return self.rng[(s.id, "fail")].expovariate(1.0 / s.mtbf)

    def _ev(self, delay: float, kind: int, si: int = -1, mi: int = -1):
        self._seq += 1
        heapq.heappush(self.evq, (self.t + delay, self._seq, kind, si, mi))

    def _set(self, si: int, mi: int, state: str):
        m = self.mach[si][mi]
        if m["state"] == state:
            return
        m["state"] = state
        if state == BLOCKED:
            m["blocked_at"] = self.t
        if self.record_timeline and len(self.timeline) < self.timeline_cap:
            self.timeline.append((round(self.t, 3), si, mi, state))

    def _cap(self, si: int) -> float:
        return self.p.stages[si].in_buffer

    def _wip(self) -> int:
        w = sum(len(b) for b in self.buf)
        for row in self.mach:
            for m in row:
                if m["part"] is not None:
                    w += 1
        return w

    # ------------------------------------------------------------------
    # root-cause attribution: why is this machine not producing?
    # ------------------------------------------------------------------
    def _cause_starved(self, si: int) -> Tuple[int, str]:
        """Walk upstream through empty buffers to the first stage that is
        actually doing something (or broken). That stage owns this idle second."""
        k = si - 1
        while k >= 0:
            states = {m["state"] for m in self.mach[k]}
            if DOWN in states:
                return (k, DOWN)
            if SETUP in states:
                return (k, SETUP)
            if BUSY in states:
                return (k, BUSY)          # upstream is working, just too slow
            if BLOCKED in states:
                return (k, BLOCKED)       # pathological, but record it
            k -= 1                        # whole stage starved -> keep walking
        return (SOURCE, "SOURCE")

    def _cause_blocked(self, si: int) -> Tuple[int, str]:
        """Walk downstream through full buffers to the first stage that is
        actually consuming (or broken)."""
        k = si + 1
        while k < self.n:
            states = {m["state"] for m in self.mach[k]}
            if DOWN in states:
                return (k, DOWN)
            if SETUP in states:
                return (k, SETUP)
            if BUSY in states:
                return (k, BUSY)
            k += 1
        return (SINK, "SINK")

    def _refresh_causes(self):
        for si in range(self.n):
            for m in self.mach[si]:
                if m["state"] == STARVED:
                    m["cause"] = self._cause_starved(si)
                elif m["state"] == BLOCKED:
                    m["cause"] = self._cause_blocked(si)

    def _refresh_bn(self):
        for si in range(self.n):
            active = any(m["state"] in ACTIVE_STATES for m in self.mach[si])
            if active and self.active_since[si] is None:
                self.active_since[si] = self.t
            elif not active:
                self.active_since[si] = None

    # ------------------------------------------------------------------
    # time accrual (all statistics are integrated at event boundaries)
    # ------------------------------------------------------------------
    def _capture_frames(self, until: float):
        """Emit one frame per frame_interval up to `until`. States are constant
        between events, so every frame in the gap is a copy of the current one."""
        if self.frame_interval <= 0:
            return
        while self._next_frame_t <= until:
            counts = []
            for si in range(self.n):
                c = {BUSY: 0, SETUP: 0, DOWN: 0, BLOCKED: 0, STARVED: 0}
                for m in self.mach[si]:
                    c[m["state"]] += 1
                counts.append({
                    "s": [c[BUSY], c[SETUP], c[DOWN], c[BLOCKED], c[STARVED]],
                    "b": len(self.buf[si]),
                })
            self.frames.append({
                "t": round(self._next_frame_t, 1),
                "st": counts,
                "good": self.good,
                "scrap": sum(self.scrap),
            })
            self._next_frame_t += self.frame_interval

    def _accrue(self, dt: float):
        self._capture_frames(self.t + dt)
        for si in range(self.n):
            for m in self.mach[si]:
                st = m["state"]
                self.state_time[si][st] += dt
                if st in IDLE_STATES:
                    c_si, c_st = m["cause"]
                    self.attrib[(c_si, c_st, si, st)] += dt
            self.buf_area[si] += len(self.buf[si]) * dt
        self.wip_area += self._wip() * dt

        # Roser/Nakano/Tanaka shifting-bottleneck: the momentary bottleneck is
        # the stage whose *uninterrupted active period* is currently longest.
        best, best_since, second = None, None, None
        for si in range(self.n):
            a = self.active_since[si]
            if a is None:
                continue
            if best_since is None or a < best_since:
                second, best, best_since = best, si, a
            elif second is None or a < self.active_since[second]:
                second = si
        if best is not None:
            self.bn_time[best] += dt
            if second is not None:
                self.bn_shift += dt   # another stage's active period overlaps

    # ------------------------------------------------------------------
    # material movement
    # ------------------------------------------------------------------
    def _take_part(self, si: int) -> Optional[dict]:
        if self.buf[si]:
            part = self.buf[si].popleft()
            self._unblock_upstream(si)      # a slot just freed up
            return part
        if si == 0 and self.p.release_mode == "saturated":
            self.part_seq += 1
            self.released += 1
            return {"id": self.part_seq, "t_in": self.t}
        return None

    def _unblock_upstream(self, si: int):
        """A slot opened in buffer `si`. Hand it to the longest-blocked machine
        in stage si-1 (FIFO on block time)."""
        if si == 0:
            return
        up = si - 1
        if len(self.buf[si]) >= self._cap(si):
            return
        cands = [(m["blocked_at"], mi) for mi, m in enumerate(self.mach[up])
                 if m["state"] == BLOCKED]
        if not cands:
            return
        cands.sort()
        mi = cands[0][1]
        m = self.mach[up][mi]
        self.buf[si].append(m["part"])
        m["part"] = None
        self._set(up, mi, STARVED)
        self._try_start(up)

    def _try_start(self, si: int):
        s = self.p.stages[si]
        for mi, m in enumerate(self.mach[si]):
            if m["state"] != STARVED or m["part"] is not None:
                continue
            part = self._take_part(si)
            if part is None:
                break
            m["part"] = part
            if s.setup_time > 0 and s.batch_size > 0 and m["n_since_setup"] == 0:
                self._set(si, mi, SETUP)
                self._ev(s.setup_time, E_SETUP_END, si, mi)
            else:
                self._begin_service(si, mi)

    def _begin_service(self, si: int, mi: int):
        s = self.p.stages[si]
        m = self.mach[si][mi]
        m["remaining"] = s.proc.sample(self.rng[(s.id, "proc")])
        self._set(si, mi, BUSY)
        self._schedule_service(si, mi)

    def _schedule_service(self, si: int, mi: int):
        m = self.mach[si][mi]
        m["svc_start"] = self.t
        if m["ttf"] < m["remaining"]:
            self._ev(m["ttf"], E_FAILURE, si, mi)
        else:
            self._ev(m["remaining"], E_SERVICE_END, si, mi)

    def _push_downstream(self, si: int, mi: int):
        m = self.mach[si][mi]
        nxt = si + 1
        if nxt >= self.n:
            part = m["part"]
            m["part"] = None
            self.good += 1
            if part["t_in"] >= self.stats_start:
                self.cts.append(self.t - part["t_in"])
            self._set(si, mi, STARVED)
            self._try_start(si)
            return
        if len(self.buf[nxt]) < self._cap(nxt):
            self.buf[nxt].append(m["part"])
            m["part"] = None
            self._set(si, mi, STARVED)
            self._try_start(nxt)
            self._try_start(si)
        else:
            self._set(si, mi, BLOCKED)

    # ------------------------------------------------------------------
    # event handlers
    # ------------------------------------------------------------------
    def _on_service_end(self, si: int, mi: int):
        s = self.p.stages[si]
        m = self.mach[si][mi]
        m["ttf"] = max(0.0, m["ttf"] - (self.t - m["svc_start"]))
        m["remaining"] = 0.0
        if s.batch_size > 0:
            m["n_since_setup"] = (m["n_since_setup"] + 1) % s.batch_size
        if s.yield_rate < 1.0 and self.rng[(s.id, "qual")].random() > s.yield_rate:
            self.scrap[si] += 1
            m["part"] = None
            self._set(si, mi, STARVED)
            self._try_start(si)
            return
        self._push_downstream(si, mi)

    def _on_failure(self, si: int, mi: int):
        s = self.p.stages[si]
        m = self.mach[si][mi]
        m["remaining"] = max(0.0, m["remaining"] - (self.t - m["svc_start"]))
        m["ttf"] = 0.0
        self.failures[si] += 1
        self._set(si, mi, DOWN)
        self._ev(max(1e-6, s.repair.sample(self.rng[(s.id, "rep")])), E_REPAIR, si, mi)

    def _on_repair(self, si: int, mi: int):
        s = self.p.stages[si]
        m = self.mach[si][mi]
        m["ttf"] = self._draw_ttf(s)
        self._set(si, mi, BUSY)
        self._schedule_service(si, mi)

    def _on_arrival(self):
        if self.p.arrival is None:
            return
        self._ev(self.p.arrival.sample(self.rng_src), E_ARRIVAL)
        self.part_seq += 1
        if len(self.buf[0]) < self._cap(0):
            self.buf[0].append({"id": self.part_seq, "t_in": self.t})
            self.released += 1
            self._try_start(0)

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def run(self) -> "Simulation":
        if self.warmup > 0:
            self._ev(self.warmup, E_RESET)
        self._ev(self.horizon, E_END)
        if self.p.release_mode == "arrival" and self.p.arrival is not None:
            self._ev(self.p.arrival.sample(self.rng_src), E_ARRIVAL)
        else:
            self._try_start(0)
        for si in range(1, self.n):
            self._try_start(si)
        self._refresh_causes()
        self._refresh_bn()

        while self.evq:
            time, _seq, kind, si, mi = heapq.heappop(self.evq)
            if time > self.horizon:
                break
            dt = time - self.t
            if dt > 0:
                self._accrue(dt)
            self.t = time
            if kind == E_END:
                break
            if kind == E_RESET:
                self._zero_stats()
            elif kind == E_SERVICE_END:
                self._on_service_end(si, mi)
            elif kind == E_FAILURE:
                self._on_failure(si, mi)
            elif kind == E_REPAIR:
                self._on_repair(si, mi)
            elif kind == E_SETUP_END:
                self._begin_service(si, mi)
            elif kind == E_ARRIVAL:
                self._on_arrival()
            self._refresh_causes()
            self._refresh_bn()

        if self.t < self.horizon:
            self._accrue(self.horizon - self.t)
            self.t = self.horizon
        return self

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """Current instantaneous state of the line, for a live view."""
        return {
            "t": self.t,
            "good": self.good,
            "scrap": sum(self.scrap),
            "stages": [{
                "id": s.id,
                "machines": [m["state"] for m in self.mach[i]],
                "buffer": len(self.buf[i]),
                "buffer_cap": None if s.in_buffer == INF else s.in_buffer,
            } for i, s in enumerate(self.p.stages)],
        }

    @property
    def window(self) -> float:
        return self.horizon - self.stats_start
