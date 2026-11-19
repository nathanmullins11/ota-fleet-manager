"""Staged rollout rules (rollout_logic.py): stage sizes, assignment and when a stage is done."""
import pytest

pytestmark = pytest.mark.unit

IDS = [f"device-{i:03d}" for i in range(1, 11)]


@pytest.mark.parametrize("spec,count,sizes", [
    (None, 10, [10]),
    ("", 10, [10]),
    ("1,3,6", 10, [1, 3, 6]),          # the Iteration 3 final test
    ("1,3,6", 7, [1, 3, 3]),           # fewer devices: the last stage shrinks
    ("1,3,6", 2, [1, 1]),              # stages that would be empty are dropped
    ("3,3", 10, [3, 7]),               # leftover devices join the last stage
    ("10%,50%,100%", 10, [1, 4, 5]),   # share of the devices reached by each stage
    ("10%,100%", 3, [1, 2]),           # at least one device in the first stage
    (" 1 , 3 , 6 ", 10, [1, 3, 6]),
])
def test_stage_sizes(rollout_logic, spec, count, sizes):
    assert rollout_logic.stage_sizes(spec, count) == sizes


@pytest.mark.parametrize("spec,why", [
    ("1,,6", "empty stage"),
    ("0,3", "whole numbers of 1 or more"),
    ("a,b", "whole numbers"),
    ("1,50%", "not both"),
    ("50%,10%,100%", "rise from 1% to 100%"),
    ("10%,50%", "must reach 100%"),
    ("10%,150%", "rise from 1% to 100%"),
])
def test_bad_stage_plans_are_refused(rollout_logic, spec, why):
    with pytest.raises(rollout_logic.RolloutError, match=why):
        rollout_logic.stage_sizes(spec, 10)


def test_stages_are_filled_in_device_id_order(rollout_logic):
    stages = rollout_logic.assign_stages(list(reversed(IDS)), [1, 3, 6])
    assert stages["device-001"] == 1
    assert [d for d, s in sorted(stages.items()) if s == 2] == ["device-002", "device-003", "device-004"]
    assert sum(1 for s in stages.values() if s == 3) == 6
    assert rollout_logic.describe_stages(stages).startswith(
        "stage 1: device-001; stage 2: device-002, device-003, device-004; stage 3: device-005")


def test_sizes_must_match_the_devices(rollout_logic):
    with pytest.raises(ValueError):
        rollout_logic.assign_stages(IDS, [1, 3])


@pytest.mark.parametrize("devices,finished", [
    ([("updated", "healthy")], True),
    ([("updated", "healthy"), ("failed", "not_reached")], True),     # offline doesn't block
    ([("updated", "healthy"), ("failed", "failed_update")], True),   # failure within limits
    ([("updated", None)], False),                                    # still in its window
    ([("commanded", None), ("updated", "healthy")], False),          # still installing
    ([("pending", None)], False),
    ([], False),
])
def test_when_a_stage_is_finished(rollout_logic, devices, finished):
    assert rollout_logic.stage_finished(devices) is finished


def test_halted_rollout_reports_skipped_stages(rollout_logic):
    states = {"device-001": "rolled_back", "device-002": "skipped", "device-003": "skipped"}
    assert rollout_logic.outcome("halted", states) == (
        "rolled_back", "rollback_completed",
        "1 of 1 devices rolled back; 2 in later stages never received the update")
