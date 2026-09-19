"""
Live intake layer tests.

Nothing here touches the simulation engine's numerics; those are covered by
tests/test_engine.py and are deliberately left alone.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.harness import Suite, assert_true                        # noqa: E402
from dtwin.model import Dist, INF, Plant, Stage                     # noqa: E402
from dtwin.whatif import Patch, apply_patches                       # noqa: E402
from dtwin.live.events import (BLOCKED, BUSY, DOWN, IDLE, OBSERVED,  # noqa: E402
                               STARVED, STATE_CHANGE, Event,
                               MDFS_STATE_MAP, map_vendor_state)
from dtwin.live.twin import LiveTwin                                # noqa: E402
from dtwin.live.connectors import (CONNECTED, ERROR, Connector,     # noqa: E402
                                   MDFSConnector, MDFSProfile,
                                   MQTTTransport, ReplayConnector,
                                   RESTConnector, SQLConnector,
                                   validate_select)
from dtwin.live.service import (LiveService, generate_synthetic_session,  # noqa: E402
                                synthetic_replay_connector)

S = Suite("live intake tests")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def demo_plant() -> Plant:
    def st(sid, t, machines=1, buf=10.0):
        return Stage(id=sid, name=sid, machines=machines,
                     proc=Dist("det", t, 0.0), in_buffer=buf,
                     mtbf=INF, repair=Dist("det", 0.0, 0.0))
    return Plant(name="demo", stages=[st("OP10", 30), st("OP20", 40, 2),
                                      st("OP30", 25), st("OP40", 35)],
                 horizon=7200, warmup=600)


def ev(stage, machine, state, ts=1_700_000_000.0, prev=None, **kw):
    return Event(ts=ts, stage=stage, machine=machine,
                 event_type=kw.pop("event_type", STATE_CHANGE),
                 previous_state=prev, new_state=state, source="test",
                 provenance=kw.pop("provenance", OBSERVED), **kw)


# ==========================================================================
@S.check("An applied event changes machine state and is recorded in history")
def t_apply():
    tw = LiveTwin(demo_plant())
    ok, problems = tw.apply(ev("OP20", "OP20-M1", DOWN))
    assert_true(ok and not problems, f"event rejected: {problems}")
    ms = tw.machines[("OP20", "OP20-M1")]
    assert_true(ms.state == DOWN, f"state is {ms.state}")
    assert_true(len(tw.ledger) == 1, "event not written to the ledger")
    assert_true(tw.ledger[-1]["new_state"] == DOWN, "ledger lost the state")
    assert_true(tw.counters["events_applied"] == 1, "counter not incremented")


@S.check("A recovery event returns the machine to running and accrues downtime")
def t_recovery():
    tw = LiveTwin(demo_plant())
    t0 = 1_700_000_000.0
    tw.apply(ev("OP20", "OP20-M1", DOWN, ts=t0))
    tw.apply(ev("OP20", "OP20-M1", BUSY, ts=t0 + 300, prev=DOWN))
    ms = tw.machines[("OP20", "OP20-M1")]
    assert_true(ms.state == BUSY, f"did not recover, state={ms.state}")
    assert_true(abs(ms.durations.get(DOWN, 0) - 300) < 1e-6,
                f"downtime accrual wrong: {ms.durations}")
    assert_true(tw.counters["failures"] == 1, "failure not counted once")


@S.check("Malformed events are flagged and never applied")
def t_malformed():
    tw = LiveTwin(demo_plant())
    tw.apply(ev("OP20", "OP20-M1", BUSY))
    bad = [
        ev("OP20", "OP20-M1", "SPINNING"),              # unmapped state
        ev("NOPE", "NOPE-M1", DOWN),                    # unknown stage
        ev("OP20", "", DOWN),                           # no machine
        Event(ts=-5, stage="OP20", machine="OP20-M1", new_state=DOWN),
        Event(ts=1_700_000_000.0, stage="OP20", machine="OP20-M1",
              event_type="cycle", value=-3),
    ]
    for e in bad:
        ok, problems = tw.apply(e)
        assert_true(not ok, f"bad event was applied: {e}")
        assert_true(problems, "rejection carried no reason")
    assert_true(tw.machines[("OP20", "OP20-M1")].state == BUSY,
                "a rejected event corrupted the last known state")
    assert_true(tw.counters["events_rejected"] == len(bad),
                "reject counter wrong")
    assert_true(len(tw.rejected) == len(bad), "reject log not kept")


@S.check("Event history is bounded and keeps the newest events")
def t_bounded():
    tw = LiveTwin(demo_plant(), ledger_size=50)
    t0 = 1_700_000_000.0
    for i in range(500):
        tw.apply(ev("OP10", "OP10", BUSY if i % 2 else DOWN, ts=t0 + i))
    assert_true(len(tw.ledger) == 50, f"ledger grew to {len(tw.ledger)}")
    assert_true(tw.ledger[-1]["ts"] == t0 + 499, "newest event was dropped")
    assert_true(tw.counters["events_applied"] == 500, "counter lost events")


@S.check("Downtime propagation separates observed effects from inferred ones")
def t_propagation():
    tw = LiveTwin(demo_plant())
    t0 = 1_700_000_000.0
    tw.apply(ev("OP20", "OP20-M1", DOWN, ts=t0))
    tw.apply(ev("OP20", "OP20-M2", DOWN, ts=t0))
    chains = tw.propagation()
    assert_true(len(chains) == 1, "expected exactly one down stage")
    effects = {e["stage"]: e for e in chains[0]["effects"]}
    assert_true(effects["OP10"]["provenance"] == "inferred",
                "unreported upstream blocking should be inferred")
    tw.apply(ev("OP10", "OP10", BLOCKED, ts=t0 + 60))
    tw.apply(ev("OP30", "OP30", STARVED, ts=t0 + 70))
    effects = {e["stage"]: e for e in tw.propagation()[0]["effects"]}
    assert_true(effects["OP10"]["provenance"] == "observed",
                "reported blocking must be observed")
    assert_true(effects["OP30"]["effect"] == STARVED, "downstream not starved")
    assert_true("OP20 DOWN" in tw.summary_line(), tw.summary_line())


@S.check("IDLE is carried as its own state, unknown states are refused")
def t_state_map():
    assert_true(MDFS_STATE_MAP["IDLE"] == IDLE,
                "IDLE must not be folded into STARVED")
    assert_true(MDFS_STATE_MAP["RUNNING"] == BUSY, "RUNNING -> BUSY")
    assert_true(MDFS_STATE_MAP["FAULTED"] == DOWN, "FAULTED -> DOWN")
    assert_true(MDFS_STATE_MAP["BLOCKED"] == BLOCKED,
                "MDFS BLOCKED is observed, not inferred")
    state, warn = map_vendor_state("TEAPOT")
    assert_true(state is None and warn, "unknown state was silently coerced")


@S.check("A failing connector keeps the last known state and recovers on retry")
def t_connector_failure():
    class Flaky(Connector):
        kind = "flaky"

        def __init__(self):
            super().__init__("flaky", "Flaky source")
            self.fail_next = False
            self.n = 0

        def _read(self, now):
            if self.fail_next:
                raise ConnectionError("socket closed")
            self.n += 1
            return [ev("OP10", "OP10", BUSY, ts=1_700_000_000.0 + self.n)]

    tw = LiveTwin(demo_plant())
    svc = LiveService(tw)
    c = svc.add(Flaky())
    assert_true(c.state == CONNECTED, "connector did not connect")
    svc.tick()
    assert_true(tw.machines[("OP10", "OP10")].state == BUSY, "no state applied")

    c.fail_next = True
    svc.tick()
    assert_true(c.state == ERROR, f"failure not recorded, state={c.state}")
    assert_true(c.error and c.next_retry is not None, "no backoff scheduled")
    first_backoff = c.backoff
    assert_true(tw.machines[("OP10", "OP10")].state == BUSY,
                "last known state was lost when the source died")

    svc.tick(now=c.next_retry)                     # retry attempt, still broken
    assert_true(c.backoff > first_backoff, "backoff did not grow")

    c.fail_next = False
    svc.tick(now=c.next_retry)                     # retry succeeds
    assert_true(c.state == CONNECTED, "connector did not recover")
    n_before = tw.counters["events_applied"]
    svc.tick()
    assert_true(tw.counters["events_applied"] > n_before,
                "no events after reconnect")


@S.check("The replay connector paces a session and is labelled REPLAY")
def t_replay():
    plant = demo_plant()
    session = generate_synthetic_session(plant, horizon=1800.0)
    assert_true(len(session) > 20, f"engine produced only {len(session)} events")
    tw = LiveTwin(plant)
    svc = LiveService(tw)
    c = svc.add(ReplayConnector("r", "replay", session, speed=1.0))
    badge = svc.connection_badge()
    assert_true(badge["replay"] is True and badge["live"] is not True,
                "replay must never be presented as live")
    svc.tick()                                     # only offset-0 events are due
    early = tw.counters["events_applied"]
    assert_true(early < len(session), "replay ignored its clock")
    c.speed = 1e9
    svc.tick()
    assert_true(tw.counters["events_applied"] == len(session),
                "replay did not finish")
    assert_true(all(e["provenance"] == "replay" for e in tw.ledger),
                "replay events were not tagged replay")


@S.check("MDFS messages parse into canonical events through an injected profile")
def t_mdfs_parse():
    # docs/mdfs_interface.md and tests/fixtures/mdfs_capture/ were not supplied
    # with this repository, so the profile below is a TEST fixture, not the real
    # MDFS interface. The connector ships with no profile at all.
    profile = MDFSProfile(
        event_topic="test/station/+/state",
        fields={"ts": "t_ms", "stage": "station", "machine": "asset",
                "state": "state", "previous_state": "prev_state",
                "buffer": "buffer_count"},
        state_table=dict(MDFS_STATE_MAP), origin="test fixture")
    fake_msgs = [
        {"t_ms": 1700000000000, "station": "OP20", "asset": "OP20-M1",
         "prev_state": "RUNNING", "state": "FAULTED"},
        {"t_ms": 1700000005000, "station": "OP10", "asset": "OP10",
         "prev_state": "RUNNING", "state": "BLOCKED"},
        {"t_ms": 1700000006000, "station": "OP30", "asset": "OP30",
         "state": "STARVED"},
        {"t_ms": 1700000007000, "station": "OP40", "asset": "OP40",
         "state": "IDLE"},
        {"t_ms": 1700000008000, "station": "OP20", "asset": "OP20-M2",
         "state": "PLASMA"},                       # unmapped on purpose
    ]

    class FakeClient:
        def __init__(self):
            self.on_message = None
            self.subscribed = []
            self.looping = False

        def connect(self, host, port, keepalive):
            self.host = host

        def subscribe(self, topic):
            self.subscribed.append(topic)

        def loop_start(self):
            self.looping = True

        def loop_stop(self):
            self.looping = False

        def disconnect(self):
            self.looping = False

        def deliver(self, topic, payload):
            class M:
                pass
            m = M()
            m.topic, m.payload = topic, payload.encode()
            self.on_message(self, None, m)

    fake = FakeClient()
    transport = MQTTTransport("fake-host", 1883, profile.event_topic,
                              client_factory=lambda: fake)
    conn = MDFSConnector("mdfs", profile=profile, transport=transport)
    assert_true(conn.configured(), "connector with a profile reports unconfigured")
    assert_true(conn.connect(), f"connect failed: {conn.error}")
    assert_true(fake.subscribed == [profile.event_topic], "did not subscribe")

    for m in fake_msgs:
        fake.deliver("test/station/x/state", json.dumps(m))
    events = conn.poll()
    assert_true(len(events) == 5, f"parsed {len(events)} of 5 messages")
    first = events[0]
    assert_true(first.stage == "OP20" and first.machine == "OP20-M1",
                "stage/machine mapping wrong")
    assert_true(first.new_state == DOWN and first.previous_state == BUSY,
                f"state mapping wrong: {first.previous_state}->{first.new_state}")
    assert_true(abs(first.ts - 1700000000.0) < 1e-3,
                "epoch milliseconds not converted")
    assert_true(events[1].new_state == BLOCKED and
                events[1].provenance == OBSERVED,
                "MDFS-reported BLOCKED must be observed")
    assert_true(events[3].new_state == IDLE, "IDLE was coerced")
    assert_true(events[4].new_state is None and conn.unmapped.get("PLASMA") == 1,
                "unmapped state was not flagged")

    tw = LiveTwin(demo_plant())
    ok, bad = tw.apply_many(events)
    assert_true(ok == 4 and bad == 1, f"applied {ok}, rejected {bad}")


@S.check("An unconfigured MDFS connector says so instead of pretending")
def t_mdfs_unconfigured():
    conn = MDFSConnector("mdfs")
    assert_true(not conn.configured(), "connector claimed to be configured")
    st = conn.status()
    assert_true("UNCONFIGURED" in (st["unconfigured_reason"] or ""),
                "no reason given")
    assert_true(not conn.connect(), "unconfigured connector connected anyway")
    assert_true(conn.state == ERROR and conn.error, "failure not recorded")


@S.check("A reconnecting MQTT transport re-subscribes with backoff")
def t_mqtt_reconnect():
    profile = MDFSProfile("test/#", {"ts": "t", "stage": "station",
                                     "machine": "asset", "state": "state"},
                          origin="test fixture")
    calls = {"n": 0}

    class Client:
        def __init__(self):
            self.on_message = None
            self.subs = []

        def connect(self, *a):
            calls["n"] += 1
            if calls["n"] == 2:
                raise ConnectionRefusedError("broker down")

        def subscribe(self, t):
            self.subs.append(t)

        def loop_start(self):
            pass

        def loop_stop(self):
            pass

        def disconnect(self):
            pass

    transport = MQTTTransport("h", 1883, "test/#", client_factory=Client)
    conn = MDFSConnector("mdfs", profile=profile, transport=transport)
    conn.connect()
    assert_true(conn.state == CONNECTED, "first connect failed")
    conn.disconnect()
    ok = conn.connect()
    assert_true(not ok and conn.state == ERROR, "broker failure not surfaced")
    assert_true(conn.next_retry is not None and conn.backoff >= 1.0,
                "no backoff after a broker failure")
    conn.poll(now=conn.next_retry)
    assert_true(conn.state == CONNECTED, "retry did not reconnect")


@S.check("The shipped MDFS profile subscribes to all explicit topics")
def t_mdfs_profile_topics():
    profile = MDFSProfile.default()
    assert_true(len(profile.event_topics) == 6, "MDFS topic list is incomplete")
    assert_true(all("#" not in topic for topic in profile.event_topics),
                "MDFS profile uses a broad wildcard")
    transport = MQTTTransport("fake", topic=list(profile.event_topics),
                              client_factory=lambda: type("C", (), {
                                  "connect": lambda self, *a: None,
                                  "subscribe": lambda self, topic: getattr(self, "seen", setattr(self, "seen", []) or []).append(topic),
                                  "loop_start": lambda self: None,
                              })())
    transport.connect()
    assert_true(len(transport.topics) == 6, "multi-topic transport lost topics")


@S.check("MDFS event-specific payloads normalize without cumulative status counts")
def t_mdfs_event_mapping():
    conn = MDFSConnector(profile=MDFSProfile.default(),
                         transport=MQTTTransport("unused", topic=[]))
    base = {"timestamp": 1700000000, "station_id": "OP20-CNC",
            "machine_id": "OP20-CNC", "event_id": "x"}

    state = dict(base, event_type="STATION_STATE_CHANGE", state="RUNNING")
    ev_state = conn.parse_message("factory/LINE-BC-01/events/state_change",
                                  json.dumps(state))
    assert_true(ev_state.new_state == BUSY, "state event was not mapped")

    cycle = dict(base, event_id="cycle", event_type="CYCLE_COMPLETE",
                 cycle_time_sec=42.5)
    ev_cycle = conn.parse_message("factory/LINE-BC-01/events/cycle",
                                  json.dumps(cycle))
    assert_true(ev_cycle.event_type == "cycle" and ev_cycle.value == 42.5,
                "cycle_time_sec was not used")

    fault = dict(base, event_id="fault", event_type="FAULT_TRIGGERED",
                 fault_code="F-1")
    ev_fault = conn.parse_message("factory/LINE-BC-01/events/faults",
                                  json.dumps(fault))
    assert_true(ev_fault.new_state == DOWN, "fault was not mapped to DOWN")

    part = dict(base, event_id="part", event_type="PART_COMPLETED",
                quality="SCRAP")
    ev_part = conn.parse_message("factory/LINE-BC-01/events/production",
                                json.dumps(part))
    assert_true(ev_part.event_type == "counter" and ev_part.raw["kind"] == "scrap",
                "part quality was not preserved")

    status = dict(base, event_id="status-1", event_type="EQUIPMENT_STATUS",
                  state="RUNNING", parts_produced=100, parts_good=99)
    ev_status = conn.parse_message("factory/LINE-BC-01/status/equipment/OP20-CNC",
                                  json.dumps(status))
    assert_true(ev_status.event_type == STATE_CHANGE,
                "equipment status was treated as production")

    buffer = {"timestamp": 1700000000, "event_id": "buffer",
              "event_type": "BUFFER_STATUS", "buffer_id": "OP10-OP20",
              "current_level": 7}
    ev_buffer = conn.parse_message("factory/LINE-BC-01/status/buffers/OP10-OP20",
                                  json.dumps(buffer))
    assert_true(ev_buffer.event_type == "buffer" and ev_buffer.stage == "OP20-CNC",
                "buffer was mapped to the wrong stage")

    duplicate = conn.parse_message("factory/LINE-BC-01/events/production",
                                   json.dumps(part))
    assert_true(duplicate is None, "duplicate part was emitted twice")

    tw = LiveTwin(Plant(name="mdfs", stages=[
        Stage(id="OP20-CNC", name="OP20-CNC", proc=Dist("det", 1),
              in_buffer=10, mtbf=INF, repair=Dist("det", 0))]))
    tw.apply(ev_part)
    tw.apply(ev_status)
    tw.apply(ev_status)
    assert_true(tw.counters["scrap"] == 1 and tw.counters["good"] == 0,
                "cumulative equipment counters inflated production")


@S.check("MDFS rejects malformed, missing, and unknown records")
def t_mdfs_rejections():
    conn = MDFSConnector(profile=MDFSProfile.default(),
                         transport=MQTTTransport("unused", topic=[]))
    assert_true(conn.parse_message("x", "not-json") is None,
                "malformed JSON was accepted")
    assert_true(conn.parse_message("x", json.dumps({"station_id": "OP10-CNC"})) is None,
                "missing event type was accepted")
    bad_station = {"event_type": "STATION_STATE_CHANGE", "station_id": "NOPE",
                   "state": "RUNNING", "timestamp": 1700000000}
    assert_true(conn.parse_message("x", json.dumps(bad_station)) is None,
                "unknown station was accepted")
    bad_buffer = {"event_type": "BUFFER_STATUS", "buffer_id": "NOPE",
                  "current_level": 1, "timestamp": 1700000000}
    assert_true(conn.parse_message("x", json.dumps(bad_buffer)) is None,
                "unknown buffer was accepted")


@S.check("REST polling honours its interval and maps vendor states")
def t_rest():
    payload = json.dumps([
        {"station": "OP10", "asset": "OP10", "state": "RUNNING",
         "timestamp": 1700000000},
        {"station": "OP20", "asset": "OP20-M1", "state": "FAULTED",
         "timestamp": 1700000001},
    ]).encode()
    c = RESTConnector("rest", "MES", "http://localhost:1/state",
                      field_map={"stage": "station", "machine": "asset",
                                 "state": "state", "ts": "timestamp"},
                      interval=10.0, opener=lambda url: payload)
    assert_true(c.connect(), "rest connect failed")
    evs = c.poll(now=1000.0)
    assert_true(len(evs) == 2, f"got {len(evs)} events")
    assert_true(evs[1].new_state == DOWN, "state table not applied")
    assert_true(c.poll(now=1001.0) == [], "polled before the interval elapsed")
    assert_true(c.poll(now=1011.0) == [], "duplicate records re-emitted")


@S.check("The SQL connector accepts one SELECT and refuses everything else")
def t_sql():
    import sqlite3
    import tempfile
    for bad in ["DROP TABLE t", "SELECT 1; DELETE FROM t",
                "select * from t -- x", "insert into t values (1)"]:
        try:
            validate_select(bad)
            raise AssertionError(f"accepted unsafe query: {bad}")
        except ValueError:
            pass
    validate_select("SELECT a FROM t")

    path = os.path.join(tempfile.mkdtemp(), "shop.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE ev (ts REAL, stage TEXT, machine TEXT, state TEXT)")
    db.executemany("INSERT INTO ev VALUES (?,?,?,?)", [
        (1700000000.0, "OP10", "OP10", "RUNNING"),
        (1700000010.0, "OP20", "OP20-M1", "FAULTED")])
    db.commit()
    db.close()

    c = SQLConnector("sql", "Shop DB", path,
                     "SELECT ts, stage, machine, state FROM ev ORDER BY ts",
                     interval=0.0)
    assert_true(c.connect(), f"sql connect failed: {c.error}")
    evs = c.poll(now=1.0)
    assert_true(len(evs) == 2, f"read {len(evs)} rows")
    assert_true(evs[1].new_state == DOWN, "state not mapped")
    assert_true(c.poll(now=2.0) == [], "rows re-emitted on the next poll")
    try:
        c.conn.execute("INSERT INTO ev VALUES (1,2,3,4)")
        raise AssertionError("database was writable")
    except Exception:
        pass
    c.disconnect()


@S.check("Missing paho-mqtt degrades gracefully instead of breaking")
def t_mqtt_optional():
    have = MQTTTransport.library_available()
    conn = MDFSConnector("mdfs")
    st = conn.status()
    assert_true(st["mqtt_library"] == have, "library probe disagrees with itself")
    if not have:
        assert_true("install paho-mqtt" in (st["mqtt_note"] or ""),
                    "no honest note about the missing library")
        t = MQTTTransport("h", 1883, "x/#")
        try:
            t.connect()
            raise AssertionError("connected without the library installed")
        except RuntimeError as exc:
            assert_true("paho-mqtt" in str(exc), str(exc))
    # REST polling must remain usable either way
    rest = RESTConnector("rest", "MES", "http://localhost:1/x",
                         opener=lambda url: b"[]")
    assert_true(rest.connect(), "REST path broke when MQTT was unavailable")


@S.check("A what-if never mutates the live twin")
def t_whatif_isolation():
    plant = demo_plant()
    tw = LiveTwin(plant)
    tw.apply(ev("OP20", "OP20-M1", DOWN))
    before = tw.plant.stages[1].machines
    assert_true(before == 2, "fixture should start with 2 machines on OP20")

    scenario = apply_patches(tw.plant_copy(),
                             [Patch("OP20", "machines", "set", 3)])
    assert_true(scenario.stages[1].machines == 3, "what-if did not apply")
    assert_true(tw.plant.stages[1].machines == 2,
                "the live twin's plant was mutated by a what-if")
    assert_true(tw.plant is not scenario, "what-if shares the live Plant object")
    assert_true(tw.machines[("OP20", "OP20-M1")].state == DOWN,
                "live machine state changed during a what-if")


@S.check("The engine never imports the intake layer (architecture boundary)")
def t_import_boundary():
    forbidden = ("dtwin.live", "dtwin.ingest", "connectors", "ingest",
                 "mdfs", "mqtt", "paho", "sqlite3", "normalize")
    for fn in ("engine.py", "analysis.py", "whatif.py"):
        src = open(os.path.join(ROOT, "dtwin", fn)).read()
        for line in src.splitlines():
            ls = line.strip()
            if not (ls.startswith("import ") or ls.startswith("from ")):
                continue
            for bad in forbidden:
                assert_true(bad not in ls.lower(),
                            f"dtwin/{fn} imports {bad!r}: {ls}")
        low = src.lower()
        for token in ("mdfs", "paho", "mqtt", "connector"):
            assert_true(token not in low,
                        f"dtwin/{fn} contains source-specific token {token!r}")


if __name__ == "__main__":
    S.export_pytest(globals())
    p, f = S.run()
    sys.exit(0 if f == 0 else 1)
else:
    S.export_pytest(globals())
