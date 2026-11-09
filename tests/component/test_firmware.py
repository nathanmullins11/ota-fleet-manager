"""Firmware apps (firmware/<version>/main.py) run as child processes, the way the agent runs them.

The packaged tarballs are checked against these sources in tests/unit/test_release.py,
so testing the sources here tests what ships.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.component

FIRMWARE = Path(__file__).resolve().parents[2] / "firmware"
FAST = {"FIRMWARE_TICK_SECONDS": "0.05"}


def start(version: str, **env) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "main.py"], cwd=FIRMWARE / version,
                            env={**os.environ, **FAST, **env}, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def read_lines(proc: subprocess.Popen, count: int) -> list[dict]:
    return [json.loads(proc.stdout.readline()) for _ in range(count)]


@pytest.mark.parametrize("version", ["1.0", "2.0", "2.1"])
def test_reports_healthy_then_stops_cleanly_on_sigterm(version):
    proc = start(version)
    try:
        lines = read_lines(proc, 3)
        assert all(line["version"] == version for line in lines)
        assert all((line["status"], line["errors"]) == ("healthy", 0) for line in lines)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 0
    finally:
        proc.kill()
        proc.communicate()


def test_2_0_reports_uptime():
    proc = start("2.0")
    try:
        assert "uptime_seconds" in read_lines(proc, 1)[0]
    finally:
        proc.kill()
        proc.communicate()


def test_2_1_spikes_errors_then_crashes():
    proc = start("2.1", FIRMWARE_FAULT_START_SECONDS="0.3",
                 FIRMWARE_FAULT_CRASH_AFTER_SECONDS="0.3")
    started = time.monotonic()
    stdout, stderr = proc.communicate(timeout=10)
    lines = [json.loads(line) for line in stdout.splitlines()]

    assert proc.returncode == 1
    assert time.monotonic() - started < 5
    statuses = [line["status"] for line in lines]
    # Healthy first, then unhealthy until the crash; never back to healthy.
    first_bad = statuses.index("unhealthy")
    assert first_bad > 0
    assert set(statuses[first_bad:]) == {"unhealthy"}
    assert all(line["errors"] == 5 for line in lines[first_bad:])
    assert "fatal" in stderr
