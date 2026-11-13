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


# --------------------------------------------------------------------------- state

def run_once(version: str, state_dir, **env) -> tuple[int, list[dict], str]:
    """Start the firmware with a state folder, read two status lines, stop it."""
    proc = start(version, FIRMWARE_STATE_DIR=str(state_dir), **env)
    try:
        lines = []
        for _ in range(2):
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(json.loads(line))
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=5)
        return proc.returncode, lines, err
    finally:
        proc.kill()


def config(state_dir) -> dict:
    return json.loads((state_dir / "config.json").read_text())


def test_1_0_creates_schema_1_state_and_counts_boots(tmp_path):
    for boot in (1, 2):
        code, lines, _ = run_once("1.0", tmp_path)
        assert code == 0
        assert (lines[0]["state_schema"], lines[0]["boot_count"]) == (1, boot)
    assert config(tmp_path)["state_schema"] == 1 and config(tmp_path)["boot_count"] == 2


def test_1_0_refuses_state_written_by_newer_firmware(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"state_schema": 2, "boot_count": 3}))
    code, lines, err = run_once("1.0", tmp_path)
    assert code == 2 and lines == []
    assert "cannot read state schema 2" in err
    assert config(tmp_path)["state_schema"] == 2          # left untouched


@pytest.mark.parametrize("version", ["2.0", "2.1"])
def test_2_x_converts_schema_1_state_keeping_its_data(tmp_path, version):
    (tmp_path / "config.json").write_text(json.dumps(
        {"state_schema": 1, "interval_seconds": 7, "boot_count": 4, "site": "lab-3"}))
    code, lines, err = run_once(version, tmp_path)
    assert code == 0
    assert "converted state from schema 1 to 2" in err
    assert (lines[0]["state_schema"], lines[0]["boot_count"]) == (2, 5)
    saved = config(tmp_path)
    assert saved["settings"] == {"interval_seconds": 7, "units": "metric"}
    assert "interval_seconds" not in saved
    assert saved["site"] == "lab-3"                       # unknown fields are kept


def test_without_a_state_folder_nothing_is_written(tmp_path):
    proc = start("1.0", FIRMWARE_STATE_DIR="")
    try:
        assert json.loads(proc.stdout.readline())["boot_count"] == 1
    finally:
        proc.kill()
        proc.communicate()
    assert not (FIRMWARE / "1.0" / "config.json").exists()
