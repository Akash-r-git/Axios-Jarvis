"""
Canonical live event model.

An Event is the only thing a connector is allowed to emit. Everything a data
source says about the shop floor is flattened into this shape before it can
touch the twin, which is what keeps source-specific vocabulary out of the
engine.

State vocabulary
----------------
The engine's own machine states are BUSY / SETUP / DOWN / BLOCKED / STARVED
(see dtwin/engine.py). The live twin uses those names plus one extra, IDLE.

IDLE is deliberately NOT collapsed into STARVED. STARVED is a claim about
material ("this machine wants a part and there isn't one"), and the loss
waterfall treats it that way. A machine that is powered up but not scheduled
is making no such claim. Coercing IDLE -> STARVED would manufacture starvation
losses that were never observed, so IDLE is carried as its own live state and
simply contributes no engine-side loss attribution.

Unknown states are rejected and flagged, never guessed at.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

# ---- canonical live states ------------------------------------------------
BUSY = "BUSY"
SETUP = "SETUP"
DOWN = "DOWN"
BLOCKED = "BLOCKED"
STARVED = "STARVED"
IDLE = "IDLE"

LIVE_STATES = (BUSY, SETUP, DOWN, BLOCKED, STARVED, IDLE)
ENGINE_STATES = (BUSY, SETUP, DOWN, BLOCKED, STARVED)   # IDLE has no engine twin

# ---- provenance -----------------------------------------------------------
OBSERVED = "observed"
INFERRED = "inferred"
REPLAY = "replay"
PROVENANCE = (OBSERVED, INFERRED, REPLAY)

# ---- event types ----------------------------------------------------------
STATE_CHANGE = "state_change"
COUNTER = "counter"          # good/scrap part counters
BUFFER = "buffer"            # buffer level reading
DOWNTIME = "downtime"        # a completed downtime record (duration in value)
CYCLE = "cycle"              # a completed cycle time (seconds in value)
HEARTBEAT = "heartbeat"
EVENT_TYPES = (STATE_CHANGE, COUNTER, BUFFER, DOWNTIME, CYCLE, HEARTBEAT)


class EventError(ValueError):
    """Raised by Event.parse when a payload cannot be trusted."""


@dataclass
class Event:
    """One thing that happened on one machine at one instant."""
    ts: float                                  # epoch seconds, UTC
    stage: str
    machine: str
    event_type: str = STATE_CHANGE
    previous_state: Optional[str] = None
    new_state: Optional[str] = None
    source: str = "unknown"
    value: Any = None
    provenance: str = OBSERVED
    raw: Optional[dict] = field(default=None, repr=False)

    # ---- validation -------------------------------------------------------
    def problems(self) -> List[str]:
        """Deterministic validation. Empty list means the event is applicable."""
        bad: List[str] = []
        if not isinstance(self.ts, (int, float)) or self.ts != self.ts:
            bad.append("timestamp is not a number")
        elif self.ts <= 0:
            bad.append("timestamp must be positive epoch seconds")
        elif self.ts > time.time() + 86400 * 365:
            bad.append("timestamp is implausibly far in the future")
        if not self.stage:
            bad.append("missing stage")
        if not self.machine:
            bad.append("missing machine")
        if self.event_type not in EVENT_TYPES:
            bad.append(f"unknown event_type {self.event_type!r}")
        if self.provenance not in PROVENANCE:
            bad.append(f"unknown provenance {self.provenance!r}")
        if self.event_type == STATE_CHANGE:
            if self.new_state is None:
                bad.append("state_change without new_state")
            elif self.new_state not in LIVE_STATES:
                bad.append(f"unmapped state {self.new_state!r}")
            if self.previous_state is not None and \
                    self.previous_state not in LIVE_STATES:
                bad.append(f"unmapped previous state {self.previous_state!r}")
        if self.event_type in (COUNTER, BUFFER, DOWNTIME, CYCLE):
            if not isinstance(self.value, (int, float)) or \
                    isinstance(self.value, bool):
                bad.append(f"{self.event_type} requires a numeric value")
            elif self.value < 0:
                bad.append(f"{self.event_type} value must be >= 0")
            elif self.event_type in (DOWNTIME, CYCLE) and self.value > 86400 * 7:
                bad.append(f"{self.event_type} duration is impossible")
        return bad

    def valid(self) -> bool:
        return not self.problems()

    # ---- (de)serialisation ------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw", None)
        return d

    @staticmethod
    def parse(d: dict, source: str = "unknown",
              provenance: str = OBSERVED) -> "Event":
        """Build an Event from an already-normalised dict. Raises EventError if
        the payload is not even shaped like an event; soft problems (unknown
        states, impossible values) survive parsing and are caught by
        problems(), so that the twin can flag them rather than crash."""
        if not isinstance(d, dict):
            raise EventError("event payload is not an object")
        try:
            ts = float(d.get("ts", d.get("timestamp", 0)) or 0)
        except (TypeError, ValueError):
            raise EventError("event timestamp is not numeric")
        return Event(
            ts=ts,
            stage=str(d.get("stage", "") or ""),
            machine=str(d.get("machine", "") or ""),
            event_type=str(d.get("event_type", STATE_CHANGE)),
            previous_state=(str(d["previous_state"])
                            if d.get("previous_state") is not None else None),
            new_state=(str(d["new_state"])
                       if d.get("new_state") is not None else None),
            source=str(d.get("source", source)),
            value=d.get("value"),
            provenance=str(d.get("provenance", provenance)),
            raw=d.get("raw"),
        )


# ==========================================================================
# MDFS state mapping
# ==========================================================================
# MDFS (MaestroHub Digital Factory Simulator) reports six states. This is the
# ONLY place that vocabulary is translated. Unknown values are not coerced.
MDFS_STATE_MAP: Dict[str, str] = {
    "RUNNING": BUSY,
    "FAULTED": DOWN,
    "BLOCKED": BLOCKED,       # MDFS reports this directly: OBSERVED, not inferred
    "STARVED": STARVED,       # ditto
    "CHANGEOVER": SETUP,
    "IDLE": IDLE,             # kept distinct; see module docstring
}


def map_vendor_state(raw_state: str,
                     table: Optional[Dict[str, str]] = None
                     ) -> Tuple[Optional[str], Optional[str]]:
    """Translate a vendor state name into a canonical live state.

    Returns (state, warning). An unrecognised state yields (None, reason) so
    the caller can flag it instead of silently inventing a mapping.
    """
    table = MDFS_STATE_MAP if table is None else table
    if raw_state is None:
        return None, "missing state"
    key = str(raw_state).strip().upper()
    if key in table:
        return table[key], None
    return None, f"unmapped source state {raw_state!r}"
