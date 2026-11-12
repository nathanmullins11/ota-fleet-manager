"""Device agent: running real firmware as a child process (firmware_runner.py)."""
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.component

FIRMWARE = Path(__file__).resolve().parents[2] / "firmware"


@pytest.fixture(autouse=True)
def fast_firmware(monkeypatch):
    monkeypatch.setenv("FIRMWARE_TICK_SECONDS", "0.05")
    monkeypatch.setenv("FIRMWARE_FAULT_START_SECONDS", "0.4")
    monkeypatch.setenv("FIRMWARE_FAULT_CRASH_AFTER_SECONDS", "0.3")


@pytest.fixture
def runner(firmware_runner):
    r = firmware_runner.FirmwareRunner(restart_delay_seconds=0.2)
    yield r
    r.stop()


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_healthy_firmware_reports_healthy(runner, firmware_runner):
    runner.start("1.0", FIRMWARE / "1.0")
    assert runner.wait_until_started(3) is None
    time.sleep(0.3)
    assert runner.take_health() == firmware_runner.Health("healthy", 0, 0)


def test_stopping_on_purpose_is_not_a_crash(runner):
    runner.start("2.0", FIRMWARE / "2.0")
    assert runner.wait_until_started(3) is None
    runner.stop()
    time.sleep(0.4)
    assert runner.take_health().crashes == 0


def test_faulty_firmware_errors_crashes_and_is_restarted(runner):
    runner.start("2.1", FIRMWARE / "2.1")
    assert runner.wait_until_started(3) is None      # 2.1 starts healthy
    totals = {"errors": 0, "crashes": 0, "statuses": set()}

    def collect():
        h = runner.take_health()
        totals["errors"] += h.errors
        totals["crashes"] += h.crashes
        totals["statuses"].add(h.status)
        return totals["crashes"] >= 2                 # crashed, restarted, crashed again
    assert wait_for(collect, timeout=6)
    assert totals["errors"] > 0
    assert "unhealthy" in totals["statuses"]


def test_crash_is_reported_even_if_restarted_before_the_heartbeat(runner):
    """The heartbeat summarises the interval: a crash makes it unhealthy, whatever came after."""
    runner.start("2.1", FIRMWARE / "2.1")
    runner.take_health()
    time.sleep(1.2)                                   # crash at ~0.7 s, restart at ~0.9 s
    health = runner.take_health()
    assert health.crashes == 1 and health.status == "unhealthy"


def test_firmware_that_exits_at_once_did_not_start(runner, tmp_path):
    (tmp_path / "main.py").write_text("import sys\nsys.exit(3)\n")
    runner.start("9.0", tmp_path)
    assert "exited with code 3" in runner.wait_until_started(3)


def test_failed_trial_is_not_a_crash_and_not_restarted(runner, tmp_path):
    (tmp_path / "main.py").write_text("import sys\nsys.exit(3)\n")
    runner.start("9.0", tmp_path, trial=True)
    assert "exited with code 3" in runner.wait_until_started(3)
    time.sleep(0.5)                                   # past the restart delay
    health = runner.take_health()
    assert health.crashes == 0


def test_trial_that_starts_then_crashes_is_a_crash(runner):
    runner.start("2.1", FIRMWARE / "2.1", trial=True)
    assert runner.wait_until_started(3) is None
    assert wait_for(lambda: runner.take_health().crashes > 0, timeout=4)


def test_silent_firmware_did_not_start(runner, tmp_path):
    (tmp_path / "main.py").write_text("import time\ntime.sleep(30)\n")
    runner.start("9.1", tmp_path)
    assert "did not report its status within 0.5 s" in runner.wait_until_started(0.5)


def test_status_line_parsing(firmware_runner):
    parse = firmware_runner.parse_status_line
    assert parse('{"version": "2.0", "status": "degraded", "errors": 2, "x": 1}') == ("degraded", 2)
    assert parse('{"status": "healthy", "errors": -1}') == ("healthy", 0)
    assert parse('{"status": "updating"}') is None
    assert parse("firmware started") is None
