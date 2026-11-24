"""Orchestrator rollouts against a real PostgreSQL (rollouts.py: RolloutStore, RolloutRunner).

Needs OTA_TEST_DB (see tests/conftest.py). MQTT is replaced by a client that records what
would be published. Command timeouts are tested by moving updated_at back in SQL.
"""
import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

import paho.mqtt.client as mqtt
import pytest

from conftest import load_service_module

pytestmark = pytest.mark.component

SHA = {"1.0": "1" * 64, "2.0": "2" * 64, "2.1": "3" * 64}
PUBLIC_URL = "http://203.0.113.10:8080"
TIMEOUT = 120


@pytest.fixture(scope="module")
def rollouts():
    return load_service_module("orch", "rollouts")


@pytest.fixture(scope="module")
def logic():
    return load_service_module("orch", "rollout_logic")


@pytest.fixture(scope="module")
def msgs():
    return load_service_module("orch", "messages")


@dataclass
class _Info:
    rc: int


class FakeClient:
    """Records publishes. `fail` maps device IDs to the rc their publish returns."""

    def __init__(self):
        self.sent = []
        self.fail = {}

    def publish(self, topic, payload, qos=0):
        device = topic.split("/")[2]
        self.sent.append((device, json.loads(payload), qos))
        return _Info(self.fail.get(device, mqtt.MQTT_ERR_SUCCESS))

    def take(self):
        sent, self.sent = self.sent, []
        return sent


@pytest.fixture
def store(rollouts, orch_db, orch_registry):
    s = rollouts.RolloutStore(orch_db)
    s.sync_firmware([orch_registry.FirmwareVersion(v, sha, f"notes {v}", f"/packages/{v}")
                     for v, sha in SHA.items()])
    return s


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def runner(rollouts, store, client):
    return rollouts.RolloutRunner(store, client, PUBLIC_URL, TIMEOUT)


def add_devices(pg, *devices, version="1.0", status="healthy"):
    for d in devices:
        pg.execute("INSERT INTO devices (device_id, current_version, status, last_heartbeat) "
                   "VALUES (%s, %s, %s, now())", (d, version, status))


def lenient(logic):
    """Limits for the tests of rollout mechanics: no health window, failures tolerated."""
    return logic.HealthLimits(window_seconds=0, max_crashes=99, max_errors=999,
                              max_failed_devices=99, unresponsive_seconds=3600)


def start(store, logic, target, devices=None, rollback=None, limits=None, stages=None):
    plan = logic.plan_targets(store.devices(), target, devices, rollback)
    return store.create_rollout(target, plan, "test", limits or lenient(logic), stages)


def event(msgs, device, name, rid, version, reason=None):
    return msgs.Event(device_id=device, event=name, rollout_id=rid, version=version,
                      reason=reason, timestamp=datetime.now(UTC))


def rollout_state(pg, rid):
    return pg.execute("SELECT state FROM rollouts WHERE id = %s", (rid,)).fetchone()[0]


def device_states(store, rid):
    return store.device_states(rid)


def events(store, rid):
    return [(e, reason) for _, e, reason in store.events(rid)]


def age_commands(pg, rid, seconds):
    pg.execute("UPDATE rollout_devices SET updated_at = now() - make_interval(secs => %s) "
               "WHERE rollout_id = %s", (seconds, rid))


# --------------------------------------------------------------------------- schema

def test_schema_and_migrations_apply_and_rerun(pg):
    """The session applied init.sql, then every migration twice; crash_count must exist."""
    cols = {r[0] for r in pg.execute(
        "SELECT table_name || '.' || column_name FROM information_schema.columns "
        "WHERE column_name = 'crash_count'").fetchall()}
    assert {"devices.crash_count", "heartbeats.crash_count", "fleet_overview.crash_count"} <= cols


# --------------------------------------------------------------------------- RolloutStore

def test_sync_firmware_upserts(store, orch_registry):
    assert store.firmware()["2.0"] == (SHA["2.0"], "notes 2.0")
    store.sync_firmware([orch_registry.FirmwareVersion("2.0", "f" * 64, "rebuilt", "/packages/2.0")])
    assert store.firmware()["2.0"] == ("f" * 64, "rebuilt")
    assert set(store.firmware()) == {"1.0", "2.0", "2.1"}


def test_create_rollout_records_plan_and_reason(store, logic, pg):
    add_devices(pg, "device-001", "device-002")
    add_devices(pg, "device-003", status="offline")
    add_devices(pg, "device-004", version="2.0")
    rid = start(store, logic, "2.0")

    r = store.rollout(rid)
    assert (r["state"], r["target_version"], r["rollback_version"]) == ("pending", "2.0", "1.0")
    assert device_states(store, rid) == {"device-001": "pending", "device-002": "pending"}
    [(name, reason)] = events(store, rid)
    assert name == "started"
    assert "update to 2.0 for 2 device(s)" in reason
    assert "skipped offline: device-003" in reason and "already on 2.0: device-004" in reason


def test_only_one_rollout_at_a_time(store, logic, pg, runner, msgs):
    add_devices(pg, "device-001", "device-002")
    rid = start(store, logic, "2.0", ["device-001"])
    with pytest.raises(logic.RolloutError, match=f"rollout {rid} is still pending"):
        start(store, logic, "2.0", ["device-002"])

    runner.tick()
    runner.on_event(event(msgs, "device-001", "update_succeeded", rid, "2.0"))
    runner.tick()
    assert rollout_state(pg, rid) == "completed"
    assert start(store, logic, "2.0", ["device-002"]) == rid + 1     # allowed once finished


def test_concurrent_creates_start_only_one_rollout(rollouts, logic, orch_cfg, store, pg):
    """Two operators at the same moment: the table lock lets exactly one through."""
    add_devices(pg, "device-001")
    db_module = load_service_module("orch", "db")
    plan = logic.plan_targets(store.devices(), "2.0", None, None)
    results, barrier = [], threading.Barrier(2)

    def create():
        db = db_module.Database(orch_cfg)
        try:
            barrier.wait()
            results.append(rollouts.RolloutStore(db).create_rollout("2.0", plan, "race"))
        except logic.RolloutError as exc:
            results.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=create) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(type(r).__name__ for r in results) == ["RolloutError", "int"]
    assert pg.execute("SELECT count(*) FROM rollouts").fetchone()[0] == 1


@pytest.mark.parametrize("state", ["pending", "halted", "rolled_back"])
def test_rollback_refused_unless_in_progress_or_completed(store, logic, pg, state):
    add_devices(pg, "device-001")
    rid = start(store, logic, "2.0")
    pg.execute("UPDATE rollouts SET state = %s WHERE id = %s", (state, rid))
    with pytest.raises(logic.RolloutError, match=f"rollout {rid} is {state}"):
        store.request_rollback(rid, "no")


def test_rollback_of_unknown_rollout(store, logic):
    with pytest.raises(logic.RolloutError, match="does not exist"):
        store.request_rollback(99, "no")


def test_request_rollback_halts_and_reopens(store, logic, pg):
    add_devices(pg, "device-001")
    rid = start(store, logic, "2.0")
    pg.execute("UPDATE rollouts SET state = 'completed', ended_at = now() WHERE id = %s", (rid,))
    store.request_rollback(rid, "operator: bad build")
    r = store.rollout(rid)
    assert (r["state"], r["halt_reason"], r["ended_at"]) == ("halted", "operator: bad build", None)
    assert events(store, rid)[-1] == ("halted", "operator: bad build")


# --------------------------------------------------------------------------- RolloutRunner: update

def test_tick_sends_one_update_command_per_device(store, logic, pg, runner, client):
    add_devices(pg, "device-001", "device-002")
    rid = start(store, logic, "2.0")
    runner.tick()

    sent = client.take()
    assert sorted(d for d, _, _ in sent) == ["device-001", "device-002"]
    device, cmd, qos = sent[0]
    assert qos == 1
    assert cmd["command"] == "update" and cmd["rollout_id"] == rid
    assert cmd["target_version"] == "2.0" and cmd["sha256"] == SHA["2.0"]
    assert cmd["download_url"] == f"{PUBLIC_URL}/packages/2.0"
    assert cmd["manifest_url"] == f"{PUBLIC_URL}/packages/2.0/manifest"
    assert rollout_state(pg, rid) == "in_progress"
    assert set(device_states(store, rid).values()) == {"commanded"}

    runner.tick()
    assert client.take() == []                   # commands are sent once


def test_events_move_devices_and_complete_the_rollout(store, logic, pg, runner, msgs):
    add_devices(pg, "device-001", "device-002")
    rid = start(store, logic, "2.0")
    runner.tick()

    runner.on_event(event(msgs, "device-001", "update_started", rid, "2.0"))
    assert device_states(store, rid)["device-001"] == "commanded"
    runner.on_event(event(msgs, "device-001", "update_succeeded", rid, "2.0"))
    runner.on_event(event(msgs, "device-002", "update_failed", rid, "2.0", "checksum mismatch"))
    assert device_states(store, rid) == {"device-001": "updated", "device-002": "failed"}
    assert pg.execute("SELECT previous_version FROM devices WHERE device_id = 'device-001'"
                      ).fetchone()[0] == "1.0"

    runner.tick()
    r = store.rollout(rid)
    assert r["state"] == "completed" and r["ended_at"] is not None
    assert events(store, rid)[-1] == ("completed", "1 of 2 devices updated and healthy; failed: device-002")


def test_rollout_waits_while_devices_are_commanded(store, logic, pg, runner, msgs):
    add_devices(pg, "device-001", "device-002")
    rid = start(store, logic, "2.0")
    runner.tick()
    runner.on_event(event(msgs, "device-001", "update_succeeded", rid, "2.0"))
    runner.tick()
    assert rollout_state(pg, rid) == "in_progress"


@pytest.mark.parametrize("name,version,why", [
    ("update_succeeded", "2.1", "event is for version 2.1, expected 2.0"),
    ("rollback_succeeded", "1.0", "rollback_succeeded does not apply while the rollout is in_progress"),
])
def test_out_of_phase_events_are_ignored(store, logic, pg, runner, msgs, caplog, name, version, why):
    add_devices(pg, "device-001")
    rid = start(store, logic, "2.0")
    runner.tick()
    runner.on_event(event(msgs, "device-001", name, rid, version))
    assert device_states(store, rid) == {"device-001": "commanded"}
    assert why in caplog.text


def test_events_from_outside_the_rollout_are_ignored(store, logic, pg, runner, msgs, caplog):
    add_devices(pg, "device-001", "device-002")
    rid = start(store, logic, "2.0", ["device-001"])
    runner.tick()
    runner.on_event(event(msgs, "device-002", "update_succeeded", rid, "2.0"))   # not targeted
    runner.on_event(event(msgs, "device-001", "update_succeeded", 999, "2.0"))   # forged rollout
    assert device_states(store, rid) == {"device-001": "commanded"}
    assert caplog.text.count("device is not part of this rollout") == 2


def test_silent_device_fails_after_the_command_timeout(store, logic, pg, runner, msgs):
    add_devices(pg, "device-001", "device-002")
    rid = start(store, logic, "2.0")
    runner.tick()
    runner.on_event(event(msgs, "device-001", "update_succeeded", rid, "2.0"))

    age_commands(pg, rid, TIMEOUT - 10)          # not yet
    runner.tick()
    assert device_states(store, rid)["device-002"] == "commanded"

    age_commands(pg, rid, TIMEOUT + 1)
    runner.tick()                                # expires device-002, then finishes
    assert device_states(store, rid) == {"device-001": "updated", "device-002": "failed"}
    assert rollout_state(pg, rid) == "completed"


def test_publish_error_marks_device_failed(store, logic, pg, runner, client):
    add_devices(pg, "device-001", "device-002", "device-003")
    client.fail = {"device-002": mqtt.MQTT_ERR_PAYLOAD_SIZE,      # cannot be sent at all
                   "device-003": mqtt.MQTT_ERR_NO_CONN}           # paho queues it (QoS 1)
    rid = start(store, logic, "2.0")
    runner.tick()
    assert device_states(store, rid) == {"device-001": "commanded", "device-002": "failed",
                                         "device-003": "commanded"}


def test_restarted_orchestrator_carries_on(rollouts, store, logic, pg, runner, msgs, orch_cfg, client):
    """All rollout state is in the database, so a new runner finishes what the old one began."""
    add_devices(pg, "device-001")
    rid = start(store, logic, "2.0")
    runner.tick()
    store.db.close()                             # the old orchestrator goes away

    db = load_service_module("orch", "db").Database(orch_cfg)
    try:
        fresh = rollouts.RolloutRunner(rollouts.RolloutStore(db), client, PUBLIC_URL, TIMEOUT)
        client.take()
        fresh.tick()
        assert client.take() == []               # no duplicate commands after the restart
        fresh.on_event(event(msgs, "device-001", "update_succeeded", rid, "2.0"))
        fresh.tick()
        assert rollout_state(pg, rid) == "completed"
    finally:
        db.close()


# --------------------------------------------------------------------------- RolloutRunner: rollback

def finished_rollout(store, logic, pg, runner, msgs, devices=("device-001", "device-002")):
    add_devices(pg, *devices)
    rid = start(store, logic, "2.0")
    runner.tick()
    for d in devices:
        runner.on_event(event(msgs, d, "update_succeeded", rid, "2.0"))
    runner.tick()
    assert rollout_state(pg, rid) == "completed"
    return rid


def test_rollback_commands_every_device_once(store, logic, pg, runner, client, msgs):
    rid = finished_rollout(store, logic, pg, runner, msgs)
    client.take()
    store.request_rollback(rid, "operator: revert")
    runner.tick()

    sent = client.take()
    assert sorted(d for d, _, _ in sent) == ["device-001", "device-002"]
    assert {(c["command"], c["target_version"], c["sha256"]) for _, c, _ in sent} == \
        {("rollback", "1.0", SHA["1.0"])}
    assert events(store, rid)[-1] == ("rollback_started",
                                      "operator: revert; rolling back 2 device(s) to 1.0")
    runner.tick()
    assert client.take() == []                   # sent once


def test_rollback_completes_when_devices_restore(store, logic, pg, runner, msgs):
    rid = finished_rollout(store, logic, pg, runner, msgs)
    store.request_rollback(rid, "operator: revert")
    runner.tick()
    runner.on_event(event(msgs, "device-001", "update_succeeded", rid, "2.0"))   # stale: ignored
    for d in ("device-001", "device-002"):
        runner.on_event(event(msgs, d, "rollback_succeeded", rid, "1.0"))
    runner.tick()
    r = store.rollout(rid)
    assert r["state"] == "rolled_back" and r["ended_at"] is not None
    assert events(store, rid)[-1] == ("rollback_completed", "2 of 2 devices rolled back")


def test_rollback_with_a_silent_device_still_finishes(store, logic, pg, runner, msgs):
    rid = finished_rollout(store, logic, pg, runner, msgs)
    store.request_rollback(rid, "operator: revert")
    runner.tick()
    runner.on_event(event(msgs, "device-001", "rollback_succeeded", rid, "1.0"))
    age_commands(pg, rid, TIMEOUT + 1)
    runner.tick()
    assert device_states(store, rid) == {"device-001": "rolled_back", "device-002": "failed"}
    assert rollout_state(pg, rid) == "rolled_back"
    assert events(store, rid)[-1] == ("rollback_completed",
                                      "1 of 2 devices rolled back; failed: device-002")


def test_rollback_while_still_in_progress(store, logic, pg, runner, client, msgs):
    """A halt mid-rollout commands every targeted device, including ones that never answered."""
    add_devices(pg, "device-001", "device-002")
    rid = start(store, logic, "2.0")
    runner.tick()
    runner.on_event(event(msgs, "device-001", "update_succeeded", rid, "2.0"))
    client.take()
    store.request_rollback(rid, "operator: stop")
    runner.tick()
    assert sorted(d for d, _, _ in client.take()) == ["device-001", "device-002"]
    assert set(device_states(store, rid).values()) == {"commanded"}



# --------------------------------------------------------------------------- health evaluation

def strict(logic, **changes):
    """The orchestrator's default limits (60 s window, no crashes, 10 errors, no failures)."""
    return logic.HealthLimits().with_overrides(**changes)


def heartbeat(pg, device, version="2.0", errors=0, crashes=0, status="healthy"):
    pg.execute("INSERT INTO heartbeats (device_id, reported_version, status, error_count, "
               "crash_count, reported_at) VALUES (%s, %s, %s, %s, %s, now())",
               (device, version, status, errors, crashes))
    pg.execute("UPDATE devices SET last_heartbeat = now() WHERE device_id = %s", (device,))


def age_window(pg, rid, seconds):
    pg.execute("UPDATE rollout_devices SET window_started_at = now() - make_interval(secs => %s) "
               "WHERE rollout_id = %s", (seconds, rid))


def verdicts(store, rid):
    return {d: v for d, (v, _) in store.device_verdicts(rid).items()}


def updated(store, logic, pg, runner, msgs, devices=("device-001", "device-002"), target="2.0",
            **limits):
    """A rollout whose devices all reported update_succeeded."""
    add_devices(pg, *devices)
    rid = start(store, logic, target, limits=strict(logic, **limits))
    runner.tick()
    for d in devices:
        runner.on_event(event(msgs, d, "update_succeeded", rid, target))
    return rid


def test_limits_are_stored_with_the_rollout(store, logic, pg):
    add_devices(pg, "device-001")
    rid = start(store, logic, "2.0", limits=strict(logic, max_errors=50, window_seconds=30))
    stored = store.limits(rid, logic.HealthLimits())
    assert (stored.max_errors, stored.window_seconds, stored.max_crashes) == (50, 30, 0)
    assert "health: window 30 s, crashes <= 0, errors <= 50" in events(store, rid)[0][1]


def test_devices_are_watched_until_the_window_passes(store, logic, pg, runner, msgs):
    rid = updated(store, logic, pg, runner, msgs)
    runner.tick()
    assert verdicts(store, rid) == {} and rollout_state(pg, rid) == "in_progress"

    age_window(pg, rid, 61)
    runner.tick()
    assert verdicts(store, rid) == {"device-001": "healthy", "device-002": "healthy"}
    assert rollout_state(pg, rid) == "completed"
    assert events(store, rid)[-1] == ("completed", "2 of 2 devices updated and healthy")


def test_a_crash_rolls_back_automatically(store, logic, pg, runner, client, msgs):
    rid = updated(store, logic, pg, runner, msgs, target="2.1")
    client.take()
    heartbeat(pg, "device-002", "2.1", errors=5, crashes=1, status="unhealthy")
    runner.tick()

    r = store.rollout(rid)
    assert r["state"] == "halted"
    assert r["halt_reason"].startswith("automatic: device-002 unhealthy: crashed 1 time within")
    assert r["halt_reason"].endswith("of updating to 2.1 (limit 0)")
    # Rollback commands go out in the same tick, to every device of the rollout.
    assert sorted((d, c["command"]) for d, c, _ in client.take()) == \
        [("device-001", "rollback"), ("device-002", "rollback")]
    names = [e for e, _ in events(store, rid)]
    assert names[-3:] == ["device_verdict", "halted", "rollback_started"]


def test_too_many_errors_roll_back(store, logic, pg, runner, msgs):
    rid = updated(store, logic, pg, runner, msgs, target="2.1")
    heartbeat(pg, "device-001", "2.1", errors=5, status="unhealthy")
    runner.tick()
    assert rollout_state(pg, rid) == "in_progress"          # 5 errors: within the limit
    heartbeat(pg, "device-001", "2.1", errors=10, status="unhealthy")
    runner.tick()
    assert store.rollout(rid)["halt_reason"] == \
        "automatic: device-001 unhealthy: reported 15 errors within 0 s of updating to 2.1 (limit 10)"


def test_errors_before_the_update_do_not_count(store, logic, pg, runner, msgs):
    add_devices(pg, "device-001")
    heartbeat(pg, "device-001", "1.0", errors=50, crashes=3)  # the old firmware's trouble
    rid = start(store, logic, "2.0", limits=strict(logic))
    runner.tick()
    runner.on_event(event(msgs, "device-001", "update_succeeded", rid, "2.0"))
    runner.tick()
    assert rollout_state(pg, rid) == "in_progress" and verdicts(store, rid) == {}


def test_a_failed_install_rolls_back(store, logic, pg, runner, client, msgs):
    add_devices(pg, "device-001", "device-002")
    rid = start(store, logic, "2.0", limits=strict(logic))
    runner.tick()
    client.take()
    runner.on_event(event(msgs, "device-002", "update_failed", rid, "2.0", "checksum mismatch"))
    runner.tick()
    assert store.rollout(rid)["halt_reason"] == \
        "automatic: 1 device(s) failed to update (limit 0): device-002 (checksum mismatch)"
    # device-001 never answered yet: it is rolled back too (a no-op on the device if needed).
    assert sorted(d for d, _, _ in client.take()) == ["device-001", "device-002"]


def test_failed_installs_within_the_limit_continue(store, logic, pg, runner, msgs):
    add_devices(pg, "device-001", "device-002")
    rid = start(store, logic, "2.0", limits=strict(logic, max_failed_devices=1))
    runner.tick()
    runner.on_event(event(msgs, "device-001", "update_succeeded", rid, "2.0"))
    runner.on_event(event(msgs, "device-002", "update_failed", rid, "2.0", "download failed"))
    age_window(pg, rid, 61)
    runner.tick()
    assert rollout_state(pg, rid) == "completed"
    assert events(store, rid)[-1] == ("completed",
                                      "1 of 2 devices updated and healthy; failed: device-002")


def test_an_offline_device_never_triggers_a_rollback(store, logic, pg, runner, msgs):
    """device-002 drops off before answering: not reached, not a failed update."""
    add_devices(pg, "device-001", "device-002")
    rid = start(store, logic, "2.0", limits=strict(logic))
    runner.tick()
    runner.on_event(event(msgs, "device-001", "update_succeeded", rid, "2.0"))
    age_commands(pg, rid, TIMEOUT + 1)            # device-002: no event, no heartbeat since
    age_window(pg, rid, 61)
    heartbeat(pg, "device-001", "2.0")
    runner.tick()
    assert verdicts(store, rid) == {"device-001": "healthy", "device-002": "not_reached"}
    assert rollout_state(pg, rid) == "completed"
    assert events(store, rid)[-1][1].endswith("not reached (offline): device-002")


def test_heartbeating_without_answering_rolls_back(store, logic, pg, runner, msgs):
    add_devices(pg, "device-001", "device-002")
    rid = start(store, logic, "2.0", limits=strict(logic))
    runner.tick()
    pg.execute("UPDATE rollout_devices SET updated_at = now() - make_interval(secs => %s) "
               "WHERE rollout_id = %s AND device_id = 'device-002'", (TIMEOUT + 1, rid))
    heartbeat(pg, "device-002", "1.0")             # alive, but never replied
    runner.tick()
    assert verdicts(store, rid)["device-002"] == "no_reply"
    assert store.rollout(rid)["halt_reason"].startswith(
        "automatic: 1 device(s) failed to update (limit 0): device-002 (sent 1 heartbeat(s)")


def test_silence_after_updating_rolls_back(store, logic, pg, runner, msgs):
    rid = updated(store, logic, pg, runner, msgs)
    pg.execute("UPDATE devices SET last_heartbeat = now() - interval '50 seconds' "
               "WHERE device_id = 'device-002'")
    runner.tick()
    assert verdicts(store, rid)["device-002"] == "unresponsive"
    assert store.rollout(rid)["halt_reason"].startswith(
        "automatic: device-002 unresponsive: no heartbeat for 50 s after updating to 2.0")


def test_no_verdicts_after_the_rollout_halts(store, logic, pg, runner, msgs):
    rid = updated(store, logic, pg, runner, msgs, target="2.1")
    heartbeat(pg, "device-001", "2.1", crashes=1)
    runner.tick()
    heartbeat(pg, "device-002", "2.1", crashes=1)  # arrives after the halt
    runner.tick()
    assert verdicts(store, rid) == {"device-001": "unhealthy"}
    assert [e for e, _ in events(store, rid)].count("halted") == 1


def test_automatic_rollback_completes(store, logic, pg, runner, msgs):
    rid = updated(store, logic, pg, runner, msgs, target="2.1")
    heartbeat(pg, "device-001", "2.1", crashes=1)
    runner.tick()
    for d in ("device-001", "device-002"):
        runner.on_event(event(msgs, d, "rollback_succeeded", rid, "1.0"))
    runner.tick()
    assert rollout_state(pg, rid) == "rolled_back"
    assert events(store, rid)[-1] == ("rollback_completed", "2 of 2 devices rolled back")



# --------------------------------------------------------------------------- refused commands

def test_a_refused_forged_command_is_recorded(store, logic, pg, runner, msgs):
    add_devices(pg, "device-002")
    runner.on_event(event(msgs, "device-002", "update_failed", 900001, "2.0",
                          "checksum in manifest does not match the command"))
    kind, reason = pg.execute("SELECT kind, reason FROM rejected_messages").fetchone()
    assert kind == "refused_command"
    assert reason == ("refused an update to 2.0 for rollout 900001, which the orchestrator never "
                      "sent: checksum in manifest does not match the command")


def test_other_ignored_events_are_not_security_records(store, logic, pg, runner, msgs):
    add_devices(pg, "device-001")
    rid = start(store, logic, "2.0")
    runner.tick()
    runner.on_event(event(msgs, "device-001", "update_succeeded", 900001, "2.0"))  # unknown rollout
    runner.on_event(event(msgs, "device-001", "update_failed", rid, "2.1"))        # wrong version
    assert pg.execute("SELECT count(*) FROM rejected_messages").fetchone() == (0,)



# --------------------------------------------------------------------------- staged rollout

FLEET = [f"device-{i:03d}" for i in range(1, 11)]


def sent_to(client, command="update"):
    return sorted(d for d, c, _ in client.take() if c["command"] == command)


def succeed(runner, msgs, rid, devices, version="2.0"):
    for d in devices:
        runner.on_event(event(msgs, d, "update_succeeded", rid, version))


def stage_of(pg, rid):
    return pg.execute("SELECT current_stage, stage_count FROM rollouts WHERE id = %s",
                      (rid,)).fetchone()


def test_a_staged_rollout_commands_one_stage_at_a_time(store, logic, pg, runner, client, msgs):
    add_devices(pg, *FLEET)
    rid = start(store, logic, "2.0", limits=strict(logic, window_seconds=0), stages="1,3,6")
    assert "in 3 stages: stage 1: device-001; stage 2: device-002, device-003, device-004" \
        in events(store, rid)[0][1]

    runner.tick()
    assert sent_to(client) == ["device-001"]                    # the canary only
    assert stage_of(pg, rid) == (1, 3)
    assert sorted(d for d, s in device_states(store, rid).items() if s == "pending") == FLEET[1:]

    succeed(runner, msgs, rid, ["device-001"])
    runner.tick()                                                # canary healthy -> stage 2
    assert sent_to(client) == ["device-002", "device-003", "device-004"]
    assert stage_of(pg, rid) == (2, 3)
    assert events(store, rid)[-1] == (
        "stage_advanced", "stage 1 finished: 1 of 1 device(s) healthy; starting stage 2 of 3: "
                          "device-002, device-003, device-004")

    succeed(runner, msgs, rid, FLEET[1:4])
    runner.tick()
    assert sent_to(client) == FLEET[4:]                          # the remaining six
    succeed(runner, msgs, rid, FLEET[4:])
    runner.tick()
    assert rollout_state(pg, rid) == "completed"
    assert events(store, rid)[-1] == ("completed", "10 of 10 devices updated and healthy")


def test_the_next_stage_waits_for_the_health_window(store, logic, pg, runner, client, msgs):
    add_devices(pg, *FLEET[:4])
    rid = start(store, logic, "2.0", limits=strict(logic), stages="1,3")
    runner.tick()
    client.take()
    succeed(runner, msgs, rid, ["device-001"])
    runner.tick()
    assert client.take() == [] and stage_of(pg, rid) == (1, 2)   # still in its 60 s window
    age_window(pg, rid, 61)
    runner.tick()
    assert sent_to(client) == FLEET[1:4]


def test_a_canary_that_goes_quiet_mid_window_does_not_advance_the_stage(
        store, logic, pg, runner, client, msgs):
    """Feedback: with unresponsive_seconds (45) shorter than the window (60), a canary that
    stops reporting 21 s into its window is not passed as healthy when the window ends; it is
    judged unresponsive once its silence passes 45 s, and the rollout halts in stage 1."""
    add_devices(pg, *FLEET[:4])
    rid = start(store, logic, "2.0", limits=strict(logic), stages="1,3")
    runner.tick()
    client.take()
    succeed(runner, msgs, rid, ["device-001"])
    age_window(pg, rid, 61)
    pg.execute("UPDATE devices SET last_heartbeat = now() - interval '40 seconds' "
               "WHERE device_id = 'device-001'")
    runner.tick()
    assert client.take() == [] and stage_of(pg, rid) == (1, 2)    # not healthy: still watched
    assert verdicts(store, rid).get("device-001") is None

    age_window(pg, rid, 67)
    pg.execute("UPDATE devices SET last_heartbeat = now() - interval '46 seconds' "
               "WHERE device_id = 'device-001'")
    runner.tick()
    assert verdicts(store, rid)["device-001"] == "unresponsive"
    assert [(d, c["command"]) for d, c, _ in client.take()] == [("device-001", "rollback")]
    assert store.rollout(rid)["halt_reason"].startswith(
        "automatic (stage 1 of 2): device-001 unresponsive")


def test_a_crashing_canary_stops_the_rollout_there(store, logic, pg, runner, client, msgs):
    """Iteration 3 final test: 2.1 reaches the canary only; the rest never receive it."""
    add_devices(pg, *FLEET)
    rid = start(store, logic, "2.1", limits=strict(logic), stages="1,3,6")
    runner.tick()
    client.take()
    succeed(runner, msgs, rid, ["device-001"], "2.1")
    heartbeat(pg, "device-001", "2.1", crashes=1, status="unhealthy")
    runner.tick()

    sent = client.take()
    assert [(d, c["command"]) for d, c, _ in sent] == [("device-001", "rollback")]
    r = store.rollout(rid)
    assert r["state"] == "halted"
    assert r["halt_reason"].startswith("automatic (stage 1 of 3): device-001 unhealthy: crashed 1 time")
    assert sorted(d for d, s in device_states(store, rid).items() if s == "skipped") == FLEET[1:]

    runner.on_event(event(msgs, "device-001", "rollback_succeeded", rid, "1.0"))
    runner.tick()
    assert rollout_state(pg, rid) == "rolled_back"
    assert events(store, rid)[-1] == (
        "rollback_completed", "1 of 1 devices rolled back; 9 in later stages never received the update")


def test_a_halt_in_stage_two_rolls_back_stages_one_and_two(store, logic, pg, runner, client, msgs):
    add_devices(pg, *FLEET)
    rid = start(store, logic, "2.1", limits=strict(logic, window_seconds=0), stages="1,3,6")
    runner.tick()
    succeed(runner, msgs, rid, ["device-001"], "2.1")
    runner.tick()                                                # stage 2 starts
    client.take()
    succeed(runner, msgs, rid, FLEET[1:4], "2.1")
    heartbeat(pg, "device-003", "2.1", crashes=1)
    runner.tick()
    assert sent_to(client, "rollback") == FLEET[:4]
    assert store.rollout(rid)["halt_reason"].startswith("automatic (stage 2 of 3): device-003")
    assert sum(1 for s in device_states(store, rid).values() if s == "skipped") == 6


def test_a_device_offline_when_its_stage_starts_is_not_commanded(store, logic, pg, runner, client, msgs):
    add_devices(pg, *FLEET[:4])
    rid = start(store, logic, "2.0", limits=strict(logic, window_seconds=0), stages="1,3")
    runner.tick()
    client.take()
    # device-003 drops off while stage 1 is still running.
    pg.execute("UPDATE devices SET status = 'offline' WHERE device_id = 'device-003'")
    succeed(runner, msgs, rid, ["device-001"])
    runner.tick()
    assert sent_to(client) == ["device-002", "device-004"]
    assert verdicts(store, rid)["device-003"] == "not_reached"
    assert "offline, not commanded: device-003" in events(store, rid)[-1][1]
    succeed(runner, msgs, rid, ["device-002", "device-004"])
    runner.tick()
    assert rollout_state(pg, rid) == "completed"


def test_bad_stage_plan_creates_no_rollout(store, logic, pg):
    add_devices(pg, *FLEET[:3])
    with pytest.raises(logic.RolloutError, match="not both"):
        start(store, logic, "2.0", stages="1,50%")
    assert pg.execute("SELECT count(*) FROM rollouts").fetchone() == (0,)
