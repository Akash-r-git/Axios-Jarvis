"""
Industrial data normalizer.

Deterministic, rule-based and auditable. There is no model here and no
learning: every decision is an alias-table lookup or an arithmetic conversion,
and every decision carries a reason string so a user can disagree with it.

Input: CSV, JSON (a list of records, or a nested object containing one) and
XLSX (parsed with zipfile + xml from the standard library, size-capped).
Output: a NormalizedTable of canonical rows plus a data-quality report.

Nothing here imports the engine.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 200_000
MAX_XLSX_CELLS = 400_000

# ---------------------------------------------------------------- aliases --
ALIASES: Dict[str, Tuple[str, ...]] = {
    "machine": ("machine", "machine_id", "machineid", "asset", "asset_id",
                "assetid", "equipment", "equipment_id", "equipmentid",
                "station", "station_id", "device", "device_id", "resource",
                "unit_id", "workcenter", "work_center", "cell", "tool"),
    "stage": ("stage", "stage_id", "process", "process_step", "processstep",
              "operation", "op", "op_code", "step", "line", "line_id",
              "sequence", "seq", "area", "department", "work_step"),
    "cycle_time": ("cycle_time", "cycletime", "cycle", "processing_time",
                   "process_time", "proc_time", "duration", "run_time",
                   "runtime", "takt", "takt_time", "time_per_part",
                   "elapsed", "job_time"),
    "state": ("state", "status", "machine_state", "machine_status", "mode",
              "condition", "event_state", "op_state"),
    "timestamp": ("timestamp", "time", "ts", "datetime", "date_time",
                  "event_time", "eventtime", "recorded_at", "logged_at",
                  "start_time", "occurred_at", "date"),
    "quantity": ("quantity", "qty", "output", "units", "count", "produced",
                 "good", "good_count", "good_parts", "parts", "volume",
                 "throughput"),
    "scrap": ("scrap", "reject", "rejects", "rejected", "defects", "defect",
              "ng", "nok", "bad", "fail_count", "waste"),
    "buffer": ("buffer", "queue", "wip", "queue_length", "buffer_level",
               "queue_size", "stock", "backlog", "in_buffer", "buffer_size",
               "buffer_capacity"),
    "downtime": ("downtime", "down_time", "failure", "failures", "repair",
                 "repair_time", "breakdown", "stop_time", "stoppage",
                 "mttr", "fault_duration"),
    "uptime": ("uptime", "up_time", "availability", "running_time",
               "operating_time", "mtbf", "time_between_failures"),
    "machines": ("machines", "machine_count", "parallel", "n_machines",
                 "num_machines", "servers", "capacity_units"),
    "yield": ("yield", "yield_rate", "first_pass_yield", "fpy", "pass_rate",
              "quality_rate"),
    "previous_state": ("previous_state", "prev_state", "from_state",
                       "old_state", "state_from"),
    "order": ("order", "order_id", "job", "job_id", "batch", "batch_id",
              "lot", "serial", "part_id", "work_order"),
    "next_stage": ("next_stage", "next_station", "downstream", "successor",
                   "routes_to", "next_op"),
}

_CANON_BY_ALIAS: Dict[str, str] = {}
for _canon, _names in ALIASES.items():
    for _n in _names:
        _CANON_BY_ALIAS[_n] = _canon

# ------------------------------------------------------------ state words --
STATE_VOCAB: Dict[str, Tuple[str, ...]] = {
    "BUSY": ("busy", "running", "run", "active", "producing", "production",
             "cycling", "in_cycle", "auto", "working", "operational", "ok",
             "up", "1", "true"),
    "DOWN": ("down", "faulted", "fault", "failure", "failed", "breakdown",
             "error", "alarm", "stopped_fault", "maintenance", "repair",
             "unplanned_stop", "e_stop", "estop", "0", "false"),
    "IDLE": ("idle", "standby", "stand_by", "waiting", "off", "not_scheduled",
             "unscheduled", "paused", "stopped", "stop"),
    "BLOCKED": ("blocked", "block", "output_full", "downstream_full",
                "full", "jammed_out"),
    "STARVED": ("starved", "starving", "starve", "no_material",
                "material_wait", "input_empty", "upstream_empty", "waiting_parts"),
    "SETUP": ("setup", "set_up", "changeover", "change_over", "cleaning",
              "tool_change", "toolchange", "adjustment", "planned_stop"),
}
_STATE_BY_WORD: Dict[str, str] = {}
for _canon, _words in STATE_VOCAB.items():
    for _w in _words:
        _STATE_BY_WORD[_w] = _canon

# ------------------------------------------------------------------ units --
TIME_UNITS = {"ms": 0.001, "millisecond": 0.001, "milliseconds": 0.001,
              "msec": 0.001, "s": 1.0, "sec": 1.0, "secs": 1.0,
              "second": 1.0, "seconds": 1.0,
              "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
              "m": 60.0, "h": 3600.0, "hr": 3600.0, "hrs": 3600.0,
              "hour": 3600.0, "hours": 3600.0}
TIME_FIELDS = ("cycle_time", "downtime", "uptime")

_UNIT_RE = re.compile(r"[\(\[_ ]\s*(ms|msec|millisecond[s]?|s|sec[s]?|second[s]?|"
                      r"min[s]?|minute[s]?|h|hr[s]?|hour[s]?)\s*[\)\]]?$",
                      re.IGNORECASE)


# British/American and other harmless spelling differences, folded before the
# alias lookup so "Work Centre" and "work_center" are the same column.
_SPELLING = {"centre": "center", "organisation": "organization",
             "utilisation": "utilization", "analyse": "analyze",
             "programme": "program", "metre": "meter", "labour": "labor"}


def _clean_key(k: str) -> str:
    k = (k or "").strip().lower()
    k = re.sub(r"\(.*?\)|\[.*?\]", " ", k)
    k = re.sub(r"[^a-z0-9]+", "_", k).strip("_")
    for a, b in _SPELLING.items():
        k = k.replace(a, b)
    return k


def detect_unit(header: str, canonical: Optional[str]) -> Tuple[float, str]:
    """Return (factor to seconds, unit name) for a time-like column."""
    if canonical not in TIME_FIELDS:
        return 1.0, ""
    m = _UNIT_RE.search((header or "").strip())
    if m:
        u = m.group(1).lower()
        return TIME_UNITS.get(u, 1.0), u
    low = (header or "").lower()
    for u in ("ms", "msec", "min", "mins", "hours", "hrs", "sec", "secs"):
        if re.search(rf"(^|[_\- ]){u}($|[_\- ])", low):
            return TIME_UNITS.get(u, 1.0), u
    return 1.0, "s"


# -------------------------------------------------------------- timestamps --
_EPOCH_RE = re.compile(r"^\d{9,14}(\.\d+)?$")


def parse_timestamp(v: Any) -> Tuple[Optional[float], Optional[str]]:
    """ISO 8601, epoch seconds or epoch milliseconds -> epoch seconds."""
    if v is None or v == "":
        return None, "empty timestamp"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        f = float(v)
        return (f / 1000.0 if f > 1e11 else f), None
    s = str(v).strip()
    if _EPOCH_RE.match(s):
        f = float(s)
        return (f / 1000.0 if f > 1e11 else f), None
    iso = s.replace("Z", "+00:00")
    for candidate in (iso, iso.replace(" ", "T")):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp(), None
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%d-%m-%Y %H:%M:%S",
                "%m/%d/%Y %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(
                tzinfo=timezone.utc).timestamp(), None
        except ValueError:
            continue
    return None, f"unparseable timestamp {s!r}"


def normalize_state(v: Any) -> Tuple[Optional[str], Optional[str]]:
    if v is None or v == "":
        return None, "empty state"
    key = _clean_key(str(v))
    if key in _STATE_BY_WORD:
        return _STATE_BY_WORD[key], None
    return None, f"unknown state {str(v)[:40]!r}"


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ============================================================== readers ====
def read_csv(text: str) -> List[dict]:
    sample = text[:8000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rdr = csv.DictReader(io.StringIO(text), dialect=dialect)
    out = []
    for i, row in enumerate(rdr):
        if i >= MAX_ROWS:
            break
        out.append({k: v for k, v in row.items() if k is not None})
    return out


def read_json(text: str) -> List[dict]:
    data = json.loads(text)
    if isinstance(data, list):
        recs = data
    elif isinstance(data, dict):
        recs = None
        for key in ("records", "data", "rows", "items", "events", "results"):
            if isinstance(data.get(key), list):
                recs = data[key]
                break
        if recs is None:                     # nested: take the longest list
            lists = [v for v in data.values() if isinstance(v, list)]
            recs = max(lists, key=len) if lists else [data]
    else:
        raise ValueError("JSON payload is neither a list nor an object")
    out = []
    for r in recs[:MAX_ROWS]:
        out.append(_flatten(r) if isinstance(r, dict) else {"value": r})
    return out


def _flatten(d: dict, prefix: str = "", depth: int = 0) -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and depth < 3:
            out.update(_flatten(v, key + "_", depth + 1))
        elif isinstance(v, list):
            out[key] = json.dumps(v)[:200]
        else:
            out[key] = v
    return out


def read_xlsx(raw: bytes) -> List[dict]:
    """Minimal XLSX reader: zipfile + xml, first worksheet, shared strings.

    Deliberately small. Anything with merged cells, formulas-as-values beyond
    cached results, or more than MAX_XLSX_CELLS cells is refused rather than
    half-read.
    """
    import xml.etree.ElementTree as ET
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("not a readable .xlsx file")
    names = zf.namelist()
    sheet = next((n for n in names
                  if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")),
                 None)
    if sheet is None:
        raise ValueError("XLSX contains no worksheet")
    shared: List[str] = []
    if "xl/sharedStrings.xml" in names:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in root.findall(f"{ns}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{ns}t")))
    root = ET.fromstring(zf.read(sheet))
    rows: List[List[str]] = []
    cells = 0
    for row in root.iter(f"{ns}row"):
        vals: Dict[int, str] = {}
        for c in row.findall(f"{ns}c"):
            cells += 1
            if cells > MAX_XLSX_CELLS:
                raise ValueError("XLSX is too large for the stdlib reader; "
                                 "export it as CSV instead")
            ref = c.get("r", "")
            col = re.sub(r"\d", "", ref)
            idx = 0
            for ch in col:
                idx = idx * 26 + (ord(ch.upper()) - 64)
            v = c.find(f"{ns}v")
            text = "" if v is None else (v.text or "")
            if c.get("t") == "s":
                try:
                    text = shared[int(text)]
                except (ValueError, IndexError):
                    text = ""
            elif c.get("t") == "inlineStr":
                text = "".join(t.text or "" for t in c.iter(f"{ns}t"))
            vals[max(0, idx - 1)] = text
        if vals:
            width = max(vals) + 1
            rows.append([vals.get(i, "") for i in range(width)])
    if not rows:
        return []
    header = [h or f"col{i}" for i, h in enumerate(rows[0])]
    out = []
    for r in rows[1:MAX_ROWS]:
        out.append({header[i]: (r[i] if i < len(r) else "")
                    for i in range(len(header))})
    return out


def read_any(raw: bytes, filename: str = "") -> Tuple[List[dict], str]:
    if len(raw) > MAX_BYTES:
        raise ValueError(f"file exceeds the {MAX_BYTES // (1024*1024)} MB cap")
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".xlsx" or raw[:2] == b"PK":
        return read_xlsx(raw), "xlsx"
    text = raw.decode("utf-8-sig", "replace")
    stripped = text.lstrip()
    if ext == ".json" or stripped[:1] in ("{", "["):
        return read_json(text), "json"
    return read_csv(text), "csv"


# ============================================================ normalizer ===
@dataclass
class Column:
    source: str
    canonical: Optional[str]
    confidence: float
    reason: str
    unit: str = ""
    factor: float = 1.0

    def to_dict(self) -> dict:
        return {"source": self.source, "canonical": self.canonical,
                "confidence": round(self.confidence, 2), "reason": self.reason,
                "unit": self.unit}


@dataclass
class NormalizedTable:
    rows: List[dict] = field(default_factory=list)
    columns: List[Column] = field(default_factory=list)
    kind: str = "unknown"                 # event_log | summary | unknown
    fmt: str = ""
    source: str = ""
    quality: Dict[str, Any] = field(default_factory=dict)

    def mapping(self) -> Dict[str, str]:
        return {c.source: c.canonical for c in self.columns if c.canonical}

    def has(self, canonical: str) -> bool:
        return any(c.canonical == canonical for c in self.columns)

    def to_dict(self, sample: int = 8) -> dict:
        return {"kind": self.kind, "format": self.fmt, "source": self.source,
                "rows": len(self.rows),
                "columns": [c.to_dict() for c in self.columns],
                "quality": self.quality,
                "sample": self.rows[:sample]}


def classify_columns(headers: List[str]) -> List[Column]:
    cols = []
    for h in headers:
        key = _clean_key(h)
        canonical = _CANON_BY_ALIAS.get(key)
        conf, reason = 0.0, ""
        if canonical:
            conf = 0.95
            reason = f"'{h}' is a known alias of {canonical}"
        else:
            tokens = [t for t in key.split("_") if t]
            pairs = ["_".join(tokens[i:i + 2]) for i in range(len(tokens) - 1)]
            tok_hits = [(_CANON_BY_ALIAS[t], t) for t in pairs + tokens
                        if t in _CANON_BY_ALIAS]
            # the meaningful word wins over a short qualifier: "op status" is a
            # state column, not a stage column
            tok_hits.sort(key=lambda ca: -len(ca[1]))
            hits = [(c, a) for a, c in _CANON_BY_ALIAS.items()
                    if a in key and len(a) >= 4]
            if tok_hits:
                canonical, alias = tok_hits[0]
                conf = 0.75
                reason = f"'{h}' contains the alias token '{alias}'"
            elif hits:
                hits.sort(key=lambda ca: -len(ca[1]))
                canonical, alias = hits[0]
                conf = 0.6
                reason = f"'{h}' contains the alias '{alias}'"
            else:
                reason = f"'{h}' matched no alias; left unmapped"
        factor, unit = detect_unit(h, canonical)
        cols.append(Column(h, canonical, conf, reason, unit, factor))
    # a canonical target claimed twice: keep the most confident, demote the rest
    best: Dict[str, Column] = {}
    for c in cols:
        if not c.canonical:
            continue
        cur = best.get(c.canonical)
        if cur is None or c.confidence > cur.confidence:
            if cur is not None:
                cur.canonical, cur.confidence = None, 0.0
                cur.reason += f"; superseded by '{c.source}'"
            best[c.canonical] = c
        else:
            c.canonical, c.confidence = None, 0.0
            c.reason += f"; superseded by '{cur.source}'"
    return cols


def normalize(raw: bytes, filename: str = "",
              overrides: Optional[Dict[str, str]] = None) -> NormalizedTable:
    """Parse and canonicalise a table. `overrides` maps source column -> canonical
    target and always wins over the alias tables (that is how a user, or a
    validated LLM suggestion, corrects the mapping)."""
    records, fmt = read_any(raw, filename)
    tbl = NormalizedTable(fmt=fmt, source=filename or "uploaded data")
    if not records:
        tbl.quality = {"warnings": [{"kind": "empty", "detail":
                                     "no rows were parsed"}], "rows": 0}
        return tbl

    headers: List[str] = []
    for r in records[:200]:
        for k in r:
            if k not in headers:
                headers.append(k)
    cols = classify_columns(headers)
    if overrides:
        taken = set()
        for c in cols:
            if c.source in overrides:
                target = overrides[c.source] or None
                if target in (None, "", "ignore"):
                    c.canonical, c.confidence = None, 1.0
                    c.reason = "ignored by the user"
                else:
                    c.canonical, c.confidence = target, 1.0
                    c.reason = "set by the user"
                    _, c.unit = detect_unit(c.source, target)
                    c.factor = detect_unit(c.source, target)[0]
                taken.add(target)
        for c in cols:
            if c.source not in overrides and c.canonical in taken:
                c.canonical, c.confidence = None, 0.0
                c.reason += "; superseded by a user mapping"
    tbl.columns = cols
    colmap = {c.source: c for c in cols if c.canonical}

    warnings: List[dict] = []
    bad_ts = unknown_states = impossible = 0
    unknown_state_values: Dict[str, int] = {}
    seen: Dict[str, int] = {}
    dupes = 0
    out_rows: List[dict] = []

    for i, rec in enumerate(records):
        row: Dict[str, Any] = {"_row": i, "_source": tbl.source,
                               "_provenance": "observed"}
        for src, col in colmap.items():
            if src not in rec:
                continue
            v = rec[src]
            canon = col.canonical
            if canon == "timestamp":
                ts, err = parse_timestamp(v)
                if err:
                    bad_ts += 1
                    row["_bad_timestamp"] = str(v)[:40]
                row["timestamp"] = ts
            elif canon in ("state", "previous_state"):
                st, err = normalize_state(v)
                if err and str(v).strip() != "":
                    unknown_states += 1
                    key = str(v)[:40]
                    unknown_state_values[key] = \
                        unknown_state_values.get(key, 0) + 1
                row[canon] = st
                row[canon + "_raw"] = v
            elif canon in TIME_FIELDS:
                f = _num(v)
                row[canon] = None if f is None else f * col.factor
                if row[canon] is not None and (row[canon] < 0 or
                                               row[canon] > 86400 * 7):
                    impossible += 1
                    row["_impossible"] = canon
            elif canon in ("quantity", "scrap", "buffer", "machines", "yield"):
                f = _num(v)
                row[canon] = f
                if f is not None and (f < 0 or (canon == "yield" and f > 1.5)):
                    impossible += 1
                    row["_impossible"] = canon
            else:
                row[canon] = None if v is None else str(v).strip()
        key = json.dumps({k: v for k, v in row.items()
                          if not k.startswith("_")}, sort_keys=True,
                         default=str)
        if key in seen:
            dupes += 1
            row["_duplicate_of"] = seen[key]
        else:
            seen[key] = i
        out_rows.append(row)

    tbl.rows = out_rows

    # ---- kind ------------------------------------------------------------
    has_ts = tbl.has("timestamp")
    has_state = tbl.has("state")
    has_machine = tbl.has("machine") or tbl.has("stage")
    if has_ts and (has_state or tbl.has("cycle_time")):
        tbl.kind = "event_log"
    elif has_machine and not has_ts:
        tbl.kind = "summary"
    elif has_ts:
        tbl.kind = "event_log"
    else:
        tbl.kind = "unknown"

    # ---- warnings --------------------------------------------------------
    for need, why in (("machine", "no machine/asset column: stages cannot be "
                                  "populated with equipment"),
                      ("stage", "no stage/process column: stage order will be "
                                "inferred from machine names or first "
                                "appearance"),
                      ("timestamp", "no timestamp column: this cannot be read "
                                    "as an event log"),
                      ("cycle_time", "no cycle-time column: processing times "
                                     "must come from state durations or "
                                     "reference values")):
        if not tbl.has(need):
            warnings.append({"kind": "missing_field", "field": need,
                             "detail": why})
    if bad_ts:
        warnings.append({"kind": "bad_timestamps", "count": bad_ts,
                         "detail": f"{bad_ts} rows have timestamps that could "
                                   f"not be parsed"})
    if unknown_states:
        warnings.append({"kind": "unknown_states", "count": unknown_states,
                         "values": sorted(unknown_state_values)[:10],
                         "detail": "state values not in the vocabulary; they "
                                   "are left unmapped, not guessed"})
    if impossible:
        warnings.append({"kind": "impossible_values", "count": impossible,
                         "detail": "negative or absurd durations/quantities"})
    if dupes:
        warnings.append({"kind": "duplicates", "count": dupes,
                         "detail": f"{dupes} rows duplicate an earlier row"})
    if tbl.has("machine") and not tbl.has("stage"):
        warnings.append({"kind": "missing_relationship",
                         "detail": "machines are present but no column relates "
                                   "them to stages"})
    if tbl.has("next_stage"):
        warnings.append({"kind": "routing_column",
                         "detail": "a routing column is present; branching or "
                                   "merging routes are not supported by the "
                                   "serial engine and will be flagged"})
    unmapped = [c.source for c in cols if not c.canonical]
    if unmapped:
        warnings.append({"kind": "unmapped_columns", "count": len(unmapped),
                         "values": unmapped[:12],
                         "detail": "columns left out of the canonical model"})

    tbl.quality = {
        "rows": len(out_rows), "columns": len(cols),
        "mapped_columns": len(colmap),
        "warnings": warnings,
        "bad_timestamps": bad_ts, "unknown_states": unknown_states,
        "impossible_values": impossible, "duplicates": dupes,
        "unknown_state_values": unknown_state_values,
        "kind": tbl.kind,
    }
    return tbl
