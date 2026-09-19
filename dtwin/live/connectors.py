"""
Connectors: everything that turns an outside data source into canonical Events.

Nothing in dtwin/engine.py, analysis.py or whatif.py may import this module.
The dependency runs one way only: connectors -> events -> LiveTwin -> Plant ->
engine. tests/test_live.py enforces that with an import-boundary test.

Connection state machine
------------------------
    disconnected --connect()--> connecting --ok--> connected
         ^                          |                 |
         |                          +--fail--> error <+
         +------- disconnect() / retry ---------------+

A connector in `error` schedules a retry with exponential backoff. It never
clears the twin's last known state; a dead source means stale data, not empty
data, and the UI says which.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from dtwin.live.events import (BUFFER, COUNTER, CYCLE, DOWNTIME, OBSERVED,
                               REPLAY, STATE_CHANGE, Event, MDFS_STATE_MAP,
                               map_vendor_state)

DISCONNECTED = "disconnected"
CONNECTING = "connecting"
CONNECTED = "connected"
ERROR = "error"

BACKOFF_BASE = 1.0
BACKOFF_MAX = 30.0


class Connector:
    """Base connector. Subclasses implement _open, _close and _read."""

    kind = "base"
    #: what this connector is honestly delivering: "live" | "replay" | "poll"
    delivery = "live"

    def __init__(self, cid: str, label: str, source_label: Optional[str] = None):
        self.id = cid
        self.label = label
        self.source_label = source_label or label
        self.state = DISCONNECTED
        self.error: Optional[str] = None
        self.last_update: Optional[float] = None
        self.attempts = 0
        self.next_retry: Optional[float] = None
        self.backoff = BACKOFF_BASE
        self.events_emitted = 0
        self.notes: List[str] = []
        self._lock = threading.Lock()

    # ---- lifecycle --------------------------------------------------------
    def connect(self) -> bool:
        with self._lock:
            self.state = CONNECTING
            self.error = None
        try:
            self._open()
        except Exception as exc:
            return self._fail(f"{type(exc).__name__}: {exc}")
        with self._lock:
            self.state = CONNECTED
            self.next_retry = None
        return True

    def disconnect(self) -> None:
        try:
            self._close()
        except Exception:
            pass
        with self._lock:
            self.state = DISCONNECTED
            self.next_retry = None
            self.error = None

    def _fail(self, msg: str) -> bool:
        with self._lock:
            self.state = ERROR
            self.error = msg
            self.attempts += 1
            self.backoff = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** min(
                self.attempts - 1, 6)))
            self.next_retry = time.time() + self.backoff
        return False

    # ---- polling ----------------------------------------------------------
    def poll(self, now: Optional[float] = None) -> List[Event]:
        """Return whatever events are due. Never raises."""
        now = time.time() if now is None else now
        if self.state == ERROR:
            if self.next_retry is None or now < self.next_retry:
                return []
            if not self.connect():          # still down: backoff has grown
                return []
        if self.state != CONNECTED:
            return []
        try:
            evs = list(self._read(now))
        except Exception as exc:
            # a source that reconnects and then fails again is still failing,
            # so the backoff keeps growing until a read actually succeeds
            self._fail(f"{type(exc).__name__}: {exc}")
            return []
        if self.attempts:
            with self._lock:
                self.attempts = 0
                self.backoff = BACKOFF_BASE
        if evs:
            self.last_update = now
            self.events_emitted += len(evs)
        return evs

    # ---- status -----------------------------------------------------------
    def status(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "label": self.label,
            "source_label": self.source_label,
            "delivery": self.delivery,
            "state": self.state, "error": self.error,
            "last_update": self.last_update,
            "attempts": self.attempts,
            "next_retry_in_s": (None if self.next_retry is None
                                else max(0.0, self.next_retry - time.time())),
            "events_emitted": self.events_emitted,
            "notes": list(self.notes),
        }

    # ---- to implement -----------------------------------------------------
    def _open(self) -> None:
        pass

    def _close(self) -> None:
        pass

    def _read(self, now: float) -> Iterable[Event]:
        return ()


# ==========================================================================
# Replay
# ==========================================================================
class ReplayConnector(Connector):
    """Replays a recorded or generated session through the normal interface.

    Everything it emits carries provenance=replay and the UI labels it REPLAY.
    It is never described as live.
    """

    kind = "replay"
    delivery = "replay"

    def __init__(self, cid: str, label: str, events: List[dict],
                 speed: float = 60.0, loop: bool = False,
                 source_label: Optional[str] = None):
        super().__init__(cid, label, source_label)
        # events: dicts with "offset" (seconds from session start) + event fields
        self.events = sorted(events, key=lambda e: e.get("offset", 0.0))
        self.speed = max(0.01, float(speed))
        self.loop = loop
        self.cursor = 0
        self.t0: Optional[float] = None
        self.completed = False

    def _open(self) -> None:
        self.t0 = time.time()
        self.cursor = 0
        self.completed = False

    def _read(self, now: float) -> Iterable[Event]:
        if self.t0 is None:
            return ()
        elapsed = (now - self.t0) * self.speed
        out: List[Event] = []
        while self.cursor < len(self.events):
            rec = self.events[self.cursor]
            if rec.get("offset", 0.0) > elapsed:
                break
            d = dict(rec)
            d.pop("offset", None)
            d["ts"] = self.t0 + rec.get("offset", 0.0) / self.speed
            d.setdefault("source", self.id)
            d["provenance"] = REPLAY
            out.append(Event.parse(d, source=self.id, provenance=REPLAY))
            self.cursor += 1
        if self.cursor >= len(self.events):
            if self.loop:
                self.t0 = now
                self.cursor = 0
            else:
                self.completed = True
        return out

    def status(self) -> dict:
        s = super().status()
        s.update({"progress": (self.cursor / len(self.events))
                  if self.events else 1.0,
                  "completed": self.completed, "speed": self.speed,
                  "total_events": len(self.events)})
        return s


class FileConnector(ReplayConnector):
    """Replays an event file uploaded by the user (JSON list or JSONL).

    Records must already be canonical events (the normalizer produces them).
    Size is capped: a file big enough to exhaust memory is refused.
    """

    kind = "file"
    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self, cid: str, label: str, path: str, speed: float = 60.0):
        recs = self._load(path)
        super().__init__(cid, label, recs, speed=speed,
                         source_label=os.path.basename(path))

    @classmethod
    def _load(cls, path: str) -> List[dict]:
        if os.path.getsize(path) > cls.MAX_BYTES:
            raise ValueError(f"event file exceeds {cls.MAX_BYTES} bytes")
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        text_s = text.strip()
        if text_s.startswith("["):
            data = json.loads(text_s)
        else:
            data = [json.loads(ln) for ln in text_s.splitlines() if ln.strip()]
        if not isinstance(data, list):
            raise ValueError("event file must contain a list of events")
        base = None
        out = []
        for d in data:
            if not isinstance(d, dict):
                continue
            ts = float(d.get("ts", d.get("timestamp", 0)) or 0)
            base = ts if base is None else base
            rec = dict(d)
            rec["offset"] = max(0.0, ts - base) if base else 0.0
            out.append(rec)
        return out


# ==========================================================================
# REST polling
# ==========================================================================
class RESTConnector(Connector):
    """Polls an HTTP endpoint returning JSON records and maps them to events.

    `field_map` says which keys hold stage / machine / state / timestamp. No
    field names are guessed: an unmapped payload produces a clear error rather
    than silent nonsense.
    """

    kind = "rest"
    delivery = "poll"
    MAX_BYTES = 2 * 1024 * 1024

    def __init__(self, cid: str, label: str, url: str,
                 field_map: Optional[Dict[str, str]] = None,
                 interval: float = 5.0, records_key: Optional[str] = None,
                 state_table: Optional[Dict[str, str]] = None,
                 opener: Optional[Callable[[str], bytes]] = None):
        super().__init__(cid, label, source_label=url)
        self.url = url
        self.interval = max(0.5, float(interval))
        self.field_map = field_map or {"stage": "stage", "machine": "machine",
                                       "state": "state", "ts": "timestamp"}
        self.records_key = records_key
        self.state_table = state_table
        self.opener = opener              # injectable for tests
        self._last_poll = 0.0
        self._seen: set = set()

    def _open(self) -> None:
        if not (self.url.startswith("http://") or
                self.url.startswith("https://")):
            raise ValueError("REST url must be http(s)")

    def _fetch(self) -> Any:
        if self.opener is not None:
            raw = self.opener(self.url)
        else:
            import urllib.request
            req = urllib.request.Request(self.url,
                                         headers={"accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read(self.MAX_BYTES + 1)
        if len(raw) > self.MAX_BYTES:
            raise ValueError("REST response too large")
        return json.loads(raw.decode("utf-8", "replace"))

    def _read(self, now: float) -> Iterable[Event]:
        if now - self._last_poll < self.interval:
            return ()
        self._last_poll = now
        data = self._fetch()
        if self.records_key:
            data = data.get(self.records_key, []) if isinstance(data, dict) \
                else []
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ValueError("REST payload is not a list of records")
        out = []
        fm = self.field_map
        for rec in data[:5000]:
            if not isinstance(rec, dict):
                continue
            key = json.dumps(rec, sort_keys=True, default=str)[:400]
            if key in self._seen:
                continue
            if len(self._seen) > 20000:
                self._seen.clear()
            self._seen.add(key)
            state, warn = map_vendor_state(rec.get(fm.get("state", "state")),
                                           self.state_table)
            if warn:
                self.notes = (self.notes + [warn])[-5:]
            ts = rec.get(fm.get("ts", "timestamp"))
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                ts = now
            out.append(Event(
                ts=ts,
                stage=str(rec.get(fm.get("stage", "stage"), "")),
                machine=str(rec.get(fm.get("machine", "machine"), "")
                            or rec.get(fm.get("stage", "stage"), "")),
                event_type=STATE_CHANGE,
                new_state=state,
                source=self.id, provenance=OBSERVED, raw=rec))
        return out


# ==========================================================================
# SQL (stdlib sqlite3, read-only, single SELECT)
# ==========================================================================
_FORBIDDEN_SQL = (";", "--", "/*", "*/", "attach", "pragma", "insert",
                  "update", "delete", "drop", "alter", "create", "replace",
                  "vacuum", "begin", "commit")


def validate_select(query: str) -> str:
    """Accept exactly one read-only SELECT. Anything else is refused."""
    q = (query or "").strip().rstrip(";").strip()
    low = q.lower()
    if not low.startswith("select"):
        raise ValueError("only a single SELECT statement is allowed")
    for bad in _FORBIDDEN_SQL:
        if bad in low:
            raise ValueError(f"disallowed token in query: {bad!r}")
    return q


class SQLConnector(Connector):
    """Polls a read-only sqlite3 database with one configured SELECT.

    The interface is driver-shaped so another driver could be added later, but
    only sqlite3 is implemented, because only sqlite3 can be tested here with
    no dependencies. No unimplemented driver is exposed in the UI.
    """

    kind = "sql"
    delivery = "poll"
    driver = "sqlite3"

    def __init__(self, cid: str, label: str, path: str, query: str,
                 field_map: Optional[Dict[str, str]] = None,
                 interval: float = 5.0,
                 state_table: Optional[Dict[str, str]] = None):
        super().__init__(cid, label, source_label=f"sqlite3:{os.path.basename(path)}")
        self.path = path
        self.query = validate_select(query)
        self.field_map = field_map or {"stage": "stage", "machine": "machine",
                                       "state": "state", "ts": "ts"}
        self.interval = max(0.5, float(interval))
        self.state_table = state_table
        self.conn = None
        self._last_poll = 0.0
        self._max_ts = 0.0

    def _open(self) -> None:
        import sqlite3
        if not os.path.isfile(self.path):
            raise FileNotFoundError(self.path)
        uri = "file:" + self.path.replace("?", "%3f") + "?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def _close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _read(self, now: float) -> Iterable[Event]:
        if now - self._last_poll < self.interval:
            return ()
        self._last_poll = now
        cur = self.conn.execute(self.query)
        fm = self.field_map
        out = []
        for row in cur.fetchmany(5000):
            rec = dict(row)
            try:
                ts = float(rec.get(fm.get("ts", "ts")))
            except (TypeError, ValueError):
                continue
            if ts <= self._max_ts:
                continue
            state, warn = map_vendor_state(rec.get(fm.get("state", "state")),
                                           self.state_table)
            if warn:
                self.notes = (self.notes + [warn])[-5:]
            out.append(Event(ts=ts,
                             stage=str(rec.get(fm.get("stage", "stage"), "")),
                             machine=str(rec.get(fm.get("machine", "machine"),
                                                 "") or ""),
                             event_type=STATE_CHANGE, new_state=state,
                             source=self.id, provenance=OBSERVED, raw=rec))
        if out:
            self._max_ts = max(e.ts for e in out)
        return out


# ==========================================================================
# MDFS
# ==========================================================================
class MDFSProfile:
    """Explicit topic and field mapping for the documented MDFS line."""

    DEFAULT_TOPICS = (
        "factory/LINE-BC-01/events/state_change",
        "factory/LINE-BC-01/events/cycle",
        "factory/LINE-BC-01/events/faults",
        "factory/LINE-BC-01/events/production",
        "factory/LINE-BC-01/status/equipment/+",
        "factory/LINE-BC-01/status/buffers/+",
    )
    STATIONS = ("OP10-CNC", "OP20-CNC", "OP30-WASH", "OP40-TEST")
    BUFFER_STAGES = {
        "OP10-OP20": "OP20-CNC",
        "OP20-OP30": "OP30-WASH",
        "OP30-OP40": "OP40-TEST",
        "BUFFER-OP10-OP20": "OP20-CNC",
        "BUFFER-OP20-OP30": "OP30-WASH",
        "BUFFER-OP30-OP40": "OP40-TEST",
    }

    def __init__(self, event_topic: Any, fields: Dict[str, str],
                 rest_base: Optional[str] = None,
                 rest_paths: Optional[Dict[str, str]] = None,
                 state_table: Optional[Dict[str, str]] = None,
                 origin: str = "injected", event_topics: Optional[List[str]] = None,
                 stations: Optional[Iterable[str]] = None,
                 buffer_stages: Optional[Dict[str, str]] = None):
        self.event_topics = tuple(event_topics or
                                  ([event_topic] if isinstance(event_topic, str)
                                   else event_topic))
        self.event_topic = self.event_topics[0] if self.event_topics else ""
        self.fields = fields
        self.rest_base = rest_base
        self.rest_paths = rest_paths or {}
        self.state_table = state_table or dict(MDFS_STATE_MAP)
        self.origin = origin
        self.stations = set(stations or ())
        self.buffer_stages = dict(buffer_stages or {})

    @classmethod
    def default(cls) -> "MDFSProfile":
        return cls(
            cls.DEFAULT_TOPICS,
            fields={
                "event_type": "event_type", "ts": "timestamp",
                "station": "station_id", "machine": "machine_id",
                "state": "state", "previous_state": "previous_state",
                "cycle_time": "cycle_time_sec", "quality": "quality",
                "quantity": "quantity", "fault": "fault_code",
                "buffer": "buffer_id", "level": "current_level",
            },
            origin="documented MDFS LINE-BC-01 profile",
            stations=cls.STATIONS, buffer_stages=cls.BUFFER_STAGES)


class MQTTTransport:
    """paho-mqtt transport, lazily imported and entirely optional.

    A fake client can be injected, which is how the message-handling path is
    tested with no broker. The real socket path is unverified until it is run
    against an actual MDFS instance — see the README.
    """

    def __init__(self, host: str, port: int = 1883, topic: Any = "#",
                 username: Optional[str] = None, password: Optional[str] = None,
                 client_factory: Optional[Callable[[], Any]] = None):
        self.host, self.port = host, port
        self.topics = tuple([topic] if isinstance(topic, str) else topic)
        self.topic = self.topics[0] if self.topics else ""
        self.username, self.password = username, password
        self.client_factory = client_factory
        self.client = None
        self.messages: List[tuple] = []
        self.connected = False
        self.available_error: Optional[str] = None
        self._lock = threading.Lock()

    @staticmethod
    def library_available() -> bool:
        try:
            import paho.mqtt.client            # noqa: F401
            return True
        except Exception:
            return False

    def _make_client(self):
        if self.client_factory is not None:
            return self.client_factory()
        try:
            import paho.mqtt.client as mqtt
        except Exception as exc:
            self.available_error = ("paho-mqtt is not installed; MQTT "
                                    "transport unavailable")
            raise RuntimeError(self.available_error) from exc
        return mqtt.Client()

    def on_message(self, _client, _userdata, msg):
        payload = getattr(msg, "payload", b"")
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8", "replace")
        with self._lock:
            if len(self.messages) < 20000:
                self.messages.append((getattr(msg, "topic", ""), payload))

    def connect(self) -> None:
        c = self._make_client()
        if self.username:
            try:
                c.username_pw_set(self.username, self.password or "")
            except Exception:
                pass
        c.on_message = self.on_message
        c.connect(self.host, self.port, 30)
        for topic_name in self.topics:
            c.subscribe(topic_name)
        try:
            c.loop_start()
        except Exception:
            pass
        self.client = c
        self.connected = True

    def disconnect(self) -> None:
        if self.client is not None:
            for fn in ("loop_stop", "disconnect"):
                try:
                    getattr(self.client, fn)()
                except Exception:
                    pass
        self.client = None
        self.connected = False

    def drain(self) -> List[tuple]:
        with self._lock:
            out, self.messages = self.messages, []
        return out


class MDFSConnector(Connector):
    """MQTT connector for the documented four-station MDFS line."""

    kind = "mdfs"
    delivery = "live"

    def __init__(self, cid: str = "mdfs", label: str = "MDFS (MaestroHub DFS)",
                 profile: Optional[MDFSProfile] = None,
                 transport: Optional[MQTTTransport] = None,
                 rest_opener: Optional[Callable[[str], bytes]] = None):
        super().__init__(cid, label)
        self.profile = profile
        self.transport = transport
        self.rest_opener = rest_opener
        self.host = os.environ.get("MDFS_MQTT_HOST", "localhost")
        self.port = int(os.environ.get("MDFS_MQTT_PORT", "1883") or 1883)
        self.rest_base = os.environ.get("MDFS_REST_BASE")
        self.unmapped: Dict[str, int] = {}
        self.rest_interval = float(os.environ.get("MDFS_REST_INTERVAL", "10"))
        self._last_rest = 0.0
        if not self.configured():
            self.notes.append(self.unconfigured_reason())

        self._seen: set = set()

    @staticmethod
    def default_profile() -> MDFSProfile:
        return MDFSProfile.default()

    # ---- configuration ----------------------------------------------------
    def configured(self) -> bool:
        return self.profile is not None and (
            self.transport is not None or bool(self.host))

    def unconfigured_reason(self) -> str:
        if self.profile is None:
            return "UNCONFIGURED: no MDFS profile was supplied."
        return ("UNCONFIGURED: set MDFS_MQTT_HOST (and optionally "
                "MDFS_MQTT_PORT, MDFS_REST_BASE) to connect.")

    # ---- lifecycle --------------------------------------------------------
    def _open(self) -> None:
        if not self.configured():
            raise RuntimeError(self.unconfigured_reason())
        if self.transport is None:
            if not MQTTTransport.library_available():
                raise RuntimeError("paho-mqtt is not installed; install it or "
                                   "use REST polling")
            self.transport = MQTTTransport(
                self.host, self.port, self.profile.event_topics,
                os.environ.get("MDFS_MQTT_USER"),
                os.environ.get("MDFS_MQTT_PASSWORD"))
        self.transport.connect()

    def _close(self) -> None:
        if self.transport is not None:
            self.transport.disconnect()

    # ---- message handling -------------------------------------------------
    def parse_message(self, topic: str, payload: str) -> Optional[Event]:
        """Turn one event-type-specific MDFS message into a canonical event."""
        def reject(reason: str) -> None:
            self.notes = (self.notes + [f"MDFS message rejected: {reason}"])[-5:]

        try:
            rec = json.loads(payload)
        except (TypeError, ValueError):
            reject("malformed JSON")
            return None
        if not isinstance(rec, dict):
            reject("payload is not an object")
            return None
        event_key = rec.get("event_id", rec.get("id"))
        event_key = (f"{topic}:{event_key}" if event_key is not None else
                     f"{topic}:{json.dumps(rec, sort_keys=True, default=str)}")
        if event_key in self._seen:
            reject("duplicate event")
            return None
        self._seen.add(event_key)
        if len(self._seen) > 20000:
            self._seen.clear()
            self._seen.add(event_key)
        f = self.profile.fields
        ts = rec.get(f.get("ts", "ts"))
        try:
            ts = float(ts)
            if ts > 1e11:                    # epoch milliseconds
                ts /= 1000.0
        except (TypeError, ValueError):
            ts = time.time()

        def get(name: str, *aliases: str):
            key = f.get(name, name)
            if key in rec:
                return rec[key]
            for alias in aliases:
                if alias in rec:
                    return rec[alias]
            return None

        raw_type = get("event_type")
        topic_types = {
            "events/state_change": "STATION_STATE_CHANGE",
            "events/cycle": "CYCLE_COMPLETE",
            "events/faults": "FAULT_TRIGGERED",
            "events/production": "PART_COMPLETED",
            "status/equipment": "EQUIPMENT_STATUS",
            "status/buffers/": "BUFFER_STATUS",
        }
        etype_name = str(raw_type).upper() if raw_type else ""
        if not etype_name:
            for suffix, candidate in topic_types.items():
                if suffix in topic:
                    etype_name = candidate
                    break
        # Legacy injected profiles predate event_type and remain supported.
        if not etype_name and not self.profile.stations:
            etype_name = "STATION_STATE_CHANGE" if get("state") is not None else ""
        if not etype_name:
            reject("missing event_type")
            return None

        station = get("station", "stage")
        buffer_id = get("buffer")
        if etype_name == "BUFFER_STATUS":
            stage = self.profile.buffer_stages.get(str(buffer_id), "")
            if not stage:
                reject(f"unknown buffer_id {buffer_id!r}")
                return None
        else:
            stage = str(station or "")
        if not stage:
            reject("missing station_id")
            return None
        if self.profile.stations and stage not in self.profile.stations:
            reject(f"unknown station_id {stage!r}")
            return None
        machine = str(get("machine", "asset") or stage)

        if etype_name == "FAULT_CLEARED":
            self.notes = (self.notes + ["FAULT_CLEARED received; awaiting authoritative state"])[-5:]
            return None
        if etype_name in ("STATION_STATE_CHANGE", "EQUIPMENT_STATUS"):
            raw_state = get("state")
            if raw_state is None:
                reject("missing state")
                return None
            new_state, warn = map_vendor_state(raw_state, self.profile.state_table)
            if warn:
                key = str(raw_state)
                self.unmapped[key] = self.unmapped.get(key, 0) + 1
                self.notes = (self.notes + [warn])[-5:]
            prev_state = None
            if get("previous_state") is not None:
                prev_state, _ = map_vendor_state(get("previous_state"), self.profile.state_table)
            return Event(ts=ts, stage=stage, machine=machine,
                         event_type=STATE_CHANGE, previous_state=prev_state,
                         new_state=new_state, source=self.id,
                         provenance=OBSERVED, raw=rec)

        if etype_name == "FAULT_TRIGGERED":
            return Event(ts=ts, stage=stage, machine=machine,
                         event_type=STATE_CHANGE, new_state="DOWN",
                         source=self.id, provenance=OBSERVED, raw=rec)

        value = None
        event_type = None
        if etype_name == "CYCLE_COMPLETE":
            value, event_type = get("cycle_time", "cycle_time_sec"), CYCLE
        elif etype_name == "PART_COMPLETED":
            value, event_type = get("quantity", "count"), COUNTER
            if value is None:
                value = 1
        elif etype_name == "BUFFER_STATUS":
            value, event_type = get("level", "current_level"), BUFFER
        else:
            reject(f"unsupported event_type {etype_name!r}")
            return None
        try:
            value = float(value) if value is not None else None
        except (TypeError, ValueError):
            reject(f"invalid value for {etype_name}")
            return None
        if value is None:
            reject(f"missing required field for {etype_name}")
            return None
        if value < 0:
            reject(f"negative value for {etype_name}")
            return None
        if etype_name == "PART_COMPLETED":
            quality = str(get("quality") or "GOOD").lower()
            if quality not in ("good", "scrap", "rework"):
                reject(f"unsupported quality {quality!r}")
                return None
            rec = dict(rec)
            rec["kind"] = quality
        return Event(ts=ts, stage=stage, machine=machine, event_type=event_type,
                     source=self.id, value=value, provenance=OBSERVED, raw=rec)

    def _read(self, now: float) -> Iterable[Event]:
        out: List[Event] = []
        for topic, payload in self.transport.drain():
            ev = self.parse_message(topic, payload)
            if ev is not None:
                out.append(ev)
        if self.rest_base and now - self._last_rest >= self.rest_interval:
            self._last_rest = now
            out.extend(self._poll_rest())
        return out

    def _poll_rest(self) -> List[Event]:
        """REST polling for buffers / downtime / equipment master.

        Paths come from the profile (i.e. from docs/mdfs_interface.md). With no
        profile paths configured this is a no-op rather than a guess.
        """
        out: List[Event] = []
        for what, path in (self.profile.rest_paths or {}).items():
            url = self.rest_base.rstrip("/") + "/" + path.lstrip("/")
            try:
                if self.rest_opener is not None:
                    raw = self.rest_opener(url)
                else:
                    import urllib.request
                    with urllib.request.urlopen(url, timeout=10) as resp:
                        raw = resp.read(2 * 1024 * 1024)
                data = json.loads(raw.decode("utf-8", "replace"))
            except Exception as exc:
                self.notes = (self.notes + [f"REST {what}: {exc}"])[-5:]
                continue
            recs = data if isinstance(data, list) else data.get("items", [])
            f = self.profile.fields
            for rec in recs if isinstance(recs, list) else []:
                if not isinstance(rec, dict):
                    continue
                key = {"buffers": "buffer", "downtime": "downtime"}.get(what)
                if key is None or not f.get(key) or f[key] not in rec:
                    continue
                try:
                    val = float(rec[f[key]])
                except (TypeError, ValueError):
                    continue
                out.append(Event(
                    ts=float(rec.get(f.get("ts", "ts"), time.time()) or
                             time.time()),
                    stage=str(rec.get(f.get("stage", "stage"), "")),
                    machine=str(rec.get(f.get("machine", "machine"), "")
                                or rec.get(f.get("stage", "stage"), "")),
                    event_type=BUFFER if key == "buffer" else DOWNTIME,
                    value=val, source=self.id, provenance=OBSERVED, raw=rec))
        return out

    def status(self) -> dict:
        s = super().status()
        s.update({
            "configured": self.configured(),
            "unconfigured_reason": (None if self.configured()
                                    else self.unconfigured_reason()),
            "mqtt_library": MQTTTransport.library_available(),
            "mqtt_note": (None if MQTTTransport.library_available() else
                          "unavailable - install paho-mqtt (REST polling "
                          "still works)"),
            "unmapped_states": dict(self.unmapped),
            "profile_origin": (self.profile.origin if self.profile else None),
        })
        return s
