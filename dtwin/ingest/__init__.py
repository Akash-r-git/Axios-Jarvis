"""Ingestion: normalize -> map -> estimate -> Plant.

One-way dependency, same rule as dtwin.live: this package may import
dtwin.model / dtwin.archetypes / dtwin.calibrate, but nothing in
dtwin/engine.py, analysis.py or whatif.py may import from here.
"""
from dtwin.ingest.normalize import (ALIASES, Column, NormalizedTable,
                                    STATE_VOCAB, normalize, normalize_state,
                                    parse_timestamp, read_any)
from dtwin.ingest.mapper import (apply_edits, detect_topology, propose,
                                 to_plant, validate_proposal)
from dtwin.ingest.estimate import (build_plant, estimate_from_live,
                                   estimate_from_table)

__all__ = ["ALIASES", "STATE_VOCAB", "Column", "NormalizedTable", "normalize",
           "normalize_state", "parse_timestamp", "read_any", "propose",
           "apply_edits", "detect_topology", "to_plant", "validate_proposal",
           "build_plant", "estimate_from_table", "estimate_from_live"]
