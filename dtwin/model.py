"""
Data model for the production-line digital twin.

A Plant is an ordered list of Stages. Stage i draws work from the buffer that
sits immediately in front of it (`in_buffer`). Stage 0's buffer is the raw
material release point. The last stage feeds the sink (finished goods).

Every quantity that a what-if scenario is allowed to touch lives here, so the
what-if layer never has to reach inside the engine.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from typing import List, Optional

INF = float("inf")


# --------------------------------------------------------------------------
# Stochastic primitives
# --------------------------------------------------------------------------
@dataclass
class Dist:
    """A parametric distribution described by mean + coefficient of variation.

    CV is used rather than variance because it is the quantity shop-floor data
    actually gives you (std/mean of observed cycle times) and because it is
    scale-free, which makes what-if edits ("make stage 3 20% more variable")
    meaningful.
    """
    kind: str = "lognorm"     # det | exp | lognorm | uniform
    mean: float = 1.0
    cv: float = 0.0

    def sample(self, rng: random.Random) -> float:
        if self.mean <= 0:
            return 0.0
        if self.kind == "det" or self.cv <= 1e-12:
            return self.mean
        if self.kind == "exp":
            return rng.expovariate(1.0 / self.mean)
        if self.kind == "lognorm":
            s2 = math.log(1.0 + self.cv * self.cv)
            mu = math.log(self.mean) - 0.5 * s2
            return rng.lognormvariate(mu, math.sqrt(s2))
        if self.kind == "uniform":
            half = math.sqrt(3.0) * self.cv * self.mean
            return rng.uniform(max(1e-9, self.mean - half), self.mean + half)
        raise ValueError(f"unknown distribution kind: {self.kind!r}")

    @staticmethod
    def parse(d, default_mean: float = 0.0) -> "Dist":
        if d is None:
            return Dist("det", default_mean, 0.0)
        if isinstance(d, (int, float)):
            return Dist("det", float(d), 0.0)
        return Dist(
            str(d.get("kind", "lognorm")),
            float(d.get("mean", default_mean)),
            float(d.get("cv", 0.0)),
        )

    def to_dict(self) -> dict:
        return {"kind": self.kind, "mean": self.mean, "cv": self.cv}


# --------------------------------------------------------------------------
# Stage
# --------------------------------------------------------------------------
@dataclass
class Stage:
    id: str
    name: str
    machines: int = 1                  # parallel identical servers
    proc: Dist = field(default_factory=Dist)          # per-unit processing time (s)
    in_buffer: float = INF             # capacity of the buffer FEEDING this stage
    mtbf: float = INF                  # mean BUSY time between failures (s)
    repair: Dist = field(default_factory=lambda: Dist("lognorm", 0.0, 0.0))  # MTTR (s)
    yield_rate: float = 1.0            # fraction of units that pass (1 - scrap)
    setup_time: float = 0.0            # changeover time (s)
    batch_size: int = 0                # units between changeovers (0 = none)
    capex_per_machine: float = 0.0     # for ROI ranking of interventions
    capex_per_buffer_slot: float = 0.0

    # ---- analytical properties (closed form, no simulation needed) --------
    def availability(self) -> float:
        """A = MTBF / (MTBF + MTTR). Operation-dependent failures, so this is
        the fraction of *intended busy* time the machine is actually cutting."""
        if self.mtbf == INF or self.mtbf <= 0 or self.repair.mean <= 0:
            return 1.0
        return self.mtbf / (self.mtbf + self.repair.mean)

    def effective_unit_time(self) -> float:
        """E[time a machine is tied up per unit], inflated by breakdowns and
        amortised changeover."""
        t = self.proc.mean / self.availability()
        if self.setup_time > 0 and self.batch_size > 0:
            t += self.setup_time / self.batch_size
        return t

    def nominal_rate_per_h(self) -> float:
        """Nameplate rate: no breakdowns, no changeovers, no scrap."""
        return 3600.0 * self.machines / self.proc.mean

    def capacity_per_h(self) -> float:
        """Sustainable gross rate of this stage in isolation."""
        return 3600.0 * self.machines / self.effective_unit_time()

    # ---- (de)serialisation ------------------------------------------------
    @staticmethod
    def parse(d: dict) -> "Stage":
        buf = d.get("in_buffer", -1)
        buf = INF if (buf is None or buf < 0) else float(buf)
        mtbf = d.get("mtbf", None)
        mtbf = INF if (mtbf is None or mtbf <= 0) else float(mtbf)
        return Stage(
            id=str(d["id"]),
            name=str(d.get("name", d["id"])),
            machines=int(d.get("machines", 1)),
            proc=Dist.parse(d.get("proc")),
            in_buffer=buf,
            mtbf=mtbf,
            repair=Dist.parse(d.get("repair"), 0.0),
            yield_rate=float(d.get("yield", d.get("yield_rate", 1.0))),
            setup_time=float(d.get("setup_time", 0.0)),
            batch_size=int(d.get("batch_size", 0)),
            capex_per_machine=float(d.get("capex_per_machine", 0.0)),
            capex_per_buffer_slot=float(d.get("capex_per_buffer_slot", 0.0)),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "machines": self.machines,
            "proc": self.proc.to_dict(),
            "in_buffer": -1 if self.in_buffer == INF else self.in_buffer,
            "mtbf": -1 if self.mtbf == INF else self.mtbf,
            "repair": self.repair.to_dict(),
            "yield": self.yield_rate,
            "setup_time": self.setup_time, "batch_size": self.batch_size,
            "capex_per_machine": self.capex_per_machine,
            "capex_per_buffer_slot": self.capex_per_buffer_slot,
        }


# --------------------------------------------------------------------------
# Plant
# --------------------------------------------------------------------------
@dataclass
class Plant:
    name: str
    stages: List[Stage]
    horizon: float = 28800.0      # simulated seconds per replication
    warmup: float = 3600.0        # discarded transient (steady-state analysis)
    release_mode: str = "saturated"   # saturated | arrival
    arrival: Optional[Dist] = None    # used when release_mode == "arrival"

    def index(self, stage_id: str) -> int:
        for i, s in enumerate(self.stages):
            if s.id == stage_id:
                return i
        raise KeyError(f"no stage with id {stage_id!r}")

    def downstream_yield(self, i: int) -> float:
        """Product of yields from stage i to the sink: converts 'units started
        at stage i' into 'good units delivered'."""
        y = 1.0
        for s in self.stages[i:]:
            y *= s.yield_rate
        return y

    @staticmethod
    def parse(d: dict) -> "Plant":
        return Plant(
            name=str(d.get("name", "plant")),
            stages=[Stage.parse(s) for s in d["stages"]],
            horizon=float(d.get("horizon_s", 28800)),
            warmup=float(d.get("warmup_s", 3600)),
            release_mode=str(d.get("release_mode", "saturated")),
            arrival=Dist.parse(d["arrival"]) if d.get("arrival") else None,
        )

    @staticmethod
    def load(path: str) -> "Plant":
        with open(path) as fh:
            return Plant.parse(json.load(fh))

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "horizon_s": self.horizon,
            "warmup_s": self.warmup,
            "release_mode": self.release_mode,
            "arrival": self.arrival.to_dict() if self.arrival else None,
            "stages": [s.to_dict() for s in self.stages],
        }

    # ---- structural validation (cheap guard against garbage configs) ------
    def validate(self) -> List[str]:
        errs = []
        ids = set()
        for s in self.stages:
            if s.id in ids:
                errs.append(f"duplicate stage id {s.id!r}")
            ids.add(s.id)
            if s.machines < 1:
                errs.append(f"{s.id}: machines must be >= 1")
            if s.proc.mean <= 0:
                errs.append(f"{s.id}: processing time must be > 0")
            if not (0 < s.yield_rate <= 1):
                errs.append(f"{s.id}: yield must be in (0, 1]")
            if s.mtbf != INF and s.repair.mean <= 0:
                errs.append(f"{s.id}: finite MTBF requires MTTR > 0")
            if s.setup_time > 0 and s.batch_size <= 0:
                errs.append(f"{s.id}: setup_time requires batch_size > 0")
            if s.in_buffer != INF and s.in_buffer < 0:
                errs.append(f"{s.id}: buffer capacity must be >= 0")
        if self.warmup >= self.horizon:
            errs.append("warmup must be shorter than horizon")
        return errs
