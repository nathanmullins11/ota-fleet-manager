"""Health evaluation and automatic rollback rules (rollout_logic.py), one test per rule."""
import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def limits(rollout_logic):
    return rollout_logic.HealthLimits(window_seconds=60, max_crashes=0, max_errors=10,
                                      max_failed_devices=0, unresponsive_seconds=45)


def device(rollout_logic, since=10.0, crashes=0, errors=0, silent=2.0):
    return rollout_logic.DeviceHealth("device-003", "2.1", since, crashes, errors, silent)


# --------------------------------------------------------------------------- device verdicts

def test_still_watching_inside_the_window(rollout_logic, limits):
    assert rollout_logic.evaluate_device(device(rollout_logic, since=30), limits) is None


def test_healthy_after_the_window(rollout_logic, limits):
    verdict, reason = rollout_logic.evaluate_device(device(rollout_logic, since=63, errors=3),
                                                    limits)
    assert verdict == "healthy"
    assert reason == "within limits for 60 s on 2.1: 0 crashes, 3 errors"


def test_one_crash_is_unhealthy(rollout_logic, limits):
    assert rollout_logic.evaluate_device(device(rollout_logic, since=18, crashes=1), limits) == \
        ("unhealthy", "crashed 1 time within 18 s of updating to 2.1 (limit 0)")


def test_too_many_errors_is_unhealthy(rollout_logic, limits):
    assert rollout_logic.evaluate_device(device(rollout_logic, since=15, errors=11), limits) == \
        ("unhealthy", "reported 11 errors within 15 s of updating to 2.1 (limit 10)")


def test_errors_at_the_limit_are_allowed(rollout_logic, limits):
    assert rollout_logic.evaluate_device(device(rollout_logic, since=63, errors=10),
                                         limits)[0] == "healthy"


def test_crashes_win_over_errors(rollout_logic, limits):
    assert rollout_logic.evaluate_device(device(rollout_logic, crashes=2, errors=50),
                                         limits)[1].startswith("crashed 2 times")


def test_silence_after_updating_is_unresponsive(rollout_logic, limits):
    assert rollout_logic.evaluate_device(device(rollout_logic, since=50, silent=46), limits) == \
        ("unresponsive", "no heartbeat for 46 s after updating to 2.1 (limit 45 s)")


def test_a_short_drop_is_not_unresponsive(rollout_logic, limits):
    """Heartbeats paused for 40 s inside the window: still watching, not a failure."""
    assert rollout_logic.evaluate_device(device(rollout_logic, since=50, silent=40), limits) is None


def test_healthy_needs_the_device_to_have_been_heard_from(rollout_logic, limits):
    """Past the window: healthy once heard from after it; never heard from means keep waiting."""
    assert rollout_logic.evaluate_device(device(rollout_logic, since=70, silent=4), limits)[0] \
        == "healthy"
    assert rollout_logic.evaluate_device(device(rollout_logic, since=70, silent=None),
                                         limits) is None


@pytest.mark.parametrize("since,silent,expected", [
    (60, 0, "healthy"),          # heartbeat right at the end of the window
    (65, 5, "healthy"),          # its last heartbeat came at 60 s
    (60, 40, None),              # went quiet 20 s into the window: not healthy yet
    (64, 44, None),              # still quiet, still under the 45 s limit
    (66, 46, "unresponsive"),    # quiet for longer than allowed
    (70, 15, None),              # last heard at 55 s: the window was not yet over
])
def test_a_device_that_goes_quiet_mid_window_is_not_passed_as_healthy(
        rollout_logic, limits, since, silent, expected):
    """Feedback: unresponsive_seconds (45) shorter than the window (60) must not let a device
    that stopped reporting partway through the window be judged healthy at its end."""
    result = rollout_logic.evaluate_device(device(rollout_logic, since=since, silent=silent), limits)
    assert (result and result[0]) == expected


# --------------------------------------------------------------------------- commands not answered

def test_heartbeating_without_reply_is_a_failed_update(rollout_logic):
    assert rollout_logic.classify_no_answer(4, 120) == \
        ("no_reply", "sent 4 heartbeat(s) but no reply to the update command within 120 s")


def test_silent_without_reply_is_not_reached(rollout_logic):
    verdict, reason = rollout_logic.classify_no_answer(0, 120)
    assert verdict == "not_reached" and reason.endswith("offline")


# --------------------------------------------------------------------------- rollout decision

def test_no_verdicts_no_halt(rollout_logic, limits):
    assert rollout_logic.health_decision({}, limits) is None


def test_healthy_and_offline_devices_never_halt(rollout_logic, limits):
    verdicts = {"device-001": ("healthy", "ok"), "device-002": ("not_reached", "offline")}
    assert rollout_logic.health_decision(verdicts, limits) is None


@pytest.mark.parametrize("verdict", ["unhealthy", "unresponsive"])
def test_one_bad_device_halts(rollout_logic, limits, verdict):
    verdicts = {"device-001": ("healthy", "ok"), "device-003": (verdict, "crashed 1 time")}
    assert rollout_logic.health_decision(verdicts, limits) == \
        f"automatic: device-003 {verdict}: crashed 1 time"


@pytest.mark.parametrize("verdict", ["failed_update", "no_reply"])
def test_failed_installs_beyond_the_limit_halt(rollout_logic, limits, verdict):
    verdicts = {"device-002": (verdict, "checksum mismatch")}
    assert rollout_logic.health_decision(verdicts, limits) == \
        "automatic: 1 device(s) failed to update (limit 0): device-002 (checksum mismatch)"
    relaxed = limits.with_overrides(max_failed_devices=1)
    assert rollout_logic.health_decision(verdicts, relaxed) is None


# --------------------------------------------------------------------------- limits

def test_limits_overrides_and_validation(rollout_logic, limits):
    changed = limits.with_overrides(max_errors=50, window_seconds=None)
    assert (changed.max_errors, changed.window_seconds) == (50, 60)
    with pytest.raises(rollout_logic.RolloutError, match="max_crashes"):
        limits.with_overrides(max_crashes=-1)
    assert "errors <= 10" in limits.describe()


# --------------------------------------------------------------------------- completion

def test_rollout_waits_for_health_verdicts(rollout_logic):
    states = {"device-001": "updated", "device-002": "updated"}
    assert rollout_logic.outcome("in_progress", states, {"device-001": "healthy"}) is None
    assert rollout_logic.outcome("in_progress", states,
                                 {"device-001": "healthy", "device-002": "healthy"}) == \
        ("completed", "completed", "2 of 2 devices updated and healthy")


def test_offline_devices_are_reported_apart_from_failures(rollout_logic):
    states = {"device-001": "updated", "device-002": "failed", "device-003": "failed"}
    verdicts = {"device-001": "healthy", "device-002": "not_reached", "device-003": "failed_update"}
    assert rollout_logic.outcome("in_progress", states, verdicts)[2] == \
        "1 of 3 devices updated and healthy; failed: device-003; not reached (offline): device-002"
