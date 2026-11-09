"""Rollout rules (deploy/orchestrator/rollout_logic.py) and the registry client (registry.py)."""
import json

import pytest

pytestmark = pytest.mark.unit

SHA = "a" * 64


@pytest.fixture
def fleet(rollout_logic):
    D = rollout_logic.DeviceRow
    return [D("device-001", "1.0", "healthy"), D("device-002", "1.0", "offline"),
            D("device-003", "2.0", "healthy"), D("device-004", "1.0", "degraded")]


# --------------------------------------------------------------------------- commands

def test_update_command_matches_contract(rollout_logic):
    cmd = rollout_logic.build_command("update", 7, "2.0", SHA, "http://203.0.113.10:8080/",
                                      issued_at="2026-11-05T12:00:00Z")
    assert cmd == {
        "schema_version": 1,
        "command": "update",
        "rollout_id": 7,
        "target_version": "2.0",
        "download_url": "http://203.0.113.10:8080/packages/2.0",
        "manifest_url": "http://203.0.113.10:8080/packages/2.0/manifest",
        "signature_url": "http://203.0.113.10:8080/packages/2.0/manifest.sig",
        "sha256": SHA,
        "issued_at": "2026-11-05T12:00:00Z",
    }


def test_rollback_command_and_timestamp(rollout_logic, messages):
    cmd = rollout_logic.build_command("rollback", 7, "1.0", SHA, "http://h:8080")
    assert (cmd["command"], cmd["target_version"]) == ("rollback", "1.0")
    # issued_at follows the contract's timestamp rule (the orchestrator's own parser accepts it).
    hb = {"schema_version": 1, "device_id": "device-001", "version": "1.0", "status": "healthy",
          "error_count": 0, "timestamp": cmd["issued_at"]}
    messages.parse_heartbeat("device-001", json.dumps(hb).encode())


def test_unknown_command_rejected(rollout_logic):
    with pytest.raises(ValueError):
        rollout_logic.build_command("reboot", 1, "1.0", SHA, "http://h")


# --------------------------------------------------------------------------- targets

def test_all_skips_offline_and_devices_already_on_target(rollout_logic, fleet):
    plan = rollout_logic.plan_targets(fleet, "2.0", None, None)
    assert plan.to_command == ["device-001", "device-004"]
    assert plan.skipped_offline == ["device-002"]
    assert plan.already_on_target == ["device-003"]
    assert plan.rollback_version == "1.0"


def test_selected_devices_only(rollout_logic, fleet):
    plan = rollout_logic.plan_targets(fleet, "2.0", ["device-004", "device-001", "device-001"], None)
    assert plan.to_command == ["device-001", "device-004"]


def test_unknown_device_refused(rollout_logic, fleet):
    with pytest.raises(rollout_logic.RolloutError, match="unknown devices: device-999"):
        rollout_logic.plan_targets(fleet, "2.0", ["device-001", "device-999"], None)


def test_nothing_to_do_refused(rollout_logic, fleet):
    with pytest.raises(rollout_logic.RolloutError, match="nothing to do"):
        rollout_logic.plan_targets(fleet, "2.0", ["device-002", "device-003"], None)


def test_empty_registry_refused(rollout_logic):
    with pytest.raises(rollout_logic.RolloutError, match="no devices"):
        rollout_logic.plan_targets([], "2.0", None, None)


def test_mixed_versions_need_explicit_rollback(rollout_logic, fleet):
    # device-003 on 2.0 is a target for 2.1 together with 1.0 devices.
    with pytest.raises(rollout_logic.RolloutError, match="--rollback-to"):
        rollout_logic.plan_targets(fleet, "2.1", None, None)
    plan = rollout_logic.plan_targets(fleet, "2.1", None, "1.0")
    assert plan.to_command == ["device-001", "device-003", "device-004"]
    assert plan.rollback_version == "1.0"


def test_rollback_version_must_differ(rollout_logic, fleet):
    with pytest.raises(rollout_logic.RolloutError, match="must differ"):
        rollout_logic.plan_targets(fleet, "2.0", None, "2.0")


def test_one_active_rollout_at_a_time(rollout_logic):
    rollout_logic.check_can_start([])
    with pytest.raises(rollout_logic.RolloutError, match="rollout 4 is still in_progress"):
        rollout_logic.check_can_start([(4, "in_progress")])


@pytest.mark.parametrize("state, allowed", [("in_progress", True), ("completed", True),
                                            ("pending", False), ("halted", False),
                                            ("rolled_back", False)])
def test_which_rollouts_can_be_rolled_back(rollout_logic, state, allowed):
    if allowed:
        rollout_logic.check_can_roll_back(state, 3)
    else:
        with pytest.raises(rollout_logic.RolloutError):
            rollout_logic.check_can_roll_back(state, 3)


# --------------------------------------------------------------------------- events

@pytest.mark.parametrize("rollout_state, event, version, expected", [
    ("in_progress", "update_started", "2.0", None),
    ("in_progress", "update_succeeded", "2.0", "updated"),
    ("in_progress", "update_failed", "2.0", "failed"),
    ("completed", "update_succeeded", "2.0", "updated"),      # late answer after a timeout
    ("halted", "rollback_started", "1.0", None),
    ("halted", "rollback_succeeded", "1.0", "rolled_back"),
    ("halted", "rollback_failed", "1.0", "failed"),
])
def test_event_effects(rollout_logic, rollout_state, event, version, expected):
    new_state, ignored = rollout_logic.apply_event(rollout_state, event, version, "2.0", "1.0")
    assert (new_state, ignored) == (expected, None)


@pytest.mark.parametrize("rollout_state, event, version, reason", [
    ("halted", "update_succeeded", "2.0", "does not apply"),          # late update during rollback
    ("in_progress", "rollback_succeeded", "1.0", "does not apply"),
    ("in_progress", "update_succeeded", "2.1", "expected 2.0"),       # stale or wrong version
    ("halted", "rollback_succeeded", "2.0", "expected 1.0"),
    ("pending", "update_succeeded", "2.0", "rollout is pending"),
])
def test_events_outside_their_phase_ignored(rollout_logic, rollout_state, event, version, reason):
    new_state, ignored = rollout_logic.apply_event(rollout_state, event, version, "2.0", "1.0")
    assert new_state is None and reason in ignored


# --------------------------------------------------------------------------- outcome

def test_not_finished_while_devices_outstanding(rollout_logic):
    assert rollout_logic.outcome("in_progress", {"a": "updated", "b": "commanded"}) is None
    assert rollout_logic.outcome("halted", {"a": "rolled_back", "b": "commanded"}) is None


def test_update_completed(rollout_logic):
    assert rollout_logic.outcome("in_progress", {"a": "updated", "b": "updated"}) == \
        ("completed", "completed", "2 of 2 devices updated")


def test_update_completed_with_failures(rollout_logic):
    state, event, reason = rollout_logic.outcome(
        "in_progress", {"device-002": "failed", "device-001": "updated", "device-003": "failed"})
    assert (state, event) == ("completed", "completed")
    assert reason == "1 of 3 devices updated; failed: device-002, device-003"


def test_rollback_completed(rollout_logic):
    assert rollout_logic.outcome("halted", {"a": "rolled_back", "b": "failed"}) == \
        ("rolled_back", "rollback_completed", "1 of 2 devices rolled back; failed: b")


# --------------------------------------------------------------------------- registry client

def test_registry_versions_parsed(orch_registry):
    data = [{"version": "1.0", "sha256": SHA, "notes": "baseline", "download_url": "/packages/1.0"},
            {"version": "2.0", "sha256": "b" * 64}]
    assert orch_registry.parse_versions(data) == [
        orch_registry.FirmwareVersion("1.0", SHA, "baseline", "/packages/1.0"),
        orch_registry.FirmwareVersion("2.0", "b" * 64, "", "/packages/2.0"),
    ]


@pytest.mark.parametrize("data", [
    {"version": "1.0"},
    [{"version": "1.0\n", "sha256": SHA}],
    [{"version": "1.0", "sha256": "A" * 64}],
    [{"version": "1.0", "sha256": "abc"}],
    ["1.0"],
])
def test_bad_registry_response_rejected(orch_registry, data):
    with pytest.raises(orch_registry.RegistryError):
        orch_registry.parse_versions(data)


def test_registry_unreachable(orch_registry):
    with pytest.raises(orch_registry.RegistryError, match="cannot read"):
        orch_registry.fetch_versions("http://127.0.0.1:9", timeout=1)
