"""OTA demo firmware 2.1: the faulty update.

It looks fine at first, then fails the same way every run, so experiments are
repeatable:

  0s  to 10s   healthy, 0 errors per tick
  10s to 15s   unhealthy, 5 errors per tick
  15s          crashes (exit code 1)

The agent restarts it after a crash, so the cycle repeats until the device is rolled
back. The timings can be shortened for tests with the FIRMWARE_FAULT_* variables.

State: like 2.0, it converts a schema 1 config to schema 2 on first start, so rolling
back to 1.0 relies on the agent restoring the pre-update state snapshot.
The interface and the state policy are described in firmware/README.md.
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

VERSION = "2.1"
STATE_SCHEMA = 2
TICK_SECONDS = float(os.environ.get("FIRMWARE_TICK_SECONDS", "1"))
FAULT_START_SECONDS = float(os.environ.get("FIRMWARE_FAULT_START_SECONDS", "10"))
FAULT_CRASH_AFTER_SECONDS = float(os.environ.get("FIRMWARE_FAULT_CRASH_AFTER_SECONDS", "5"))
FAULT_ERRORS_PER_TICK = int(os.environ.get("FIRMWARE_FAULT_ERRORS_PER_TICK", "5"))
STATE_DIR = os.environ.get("FIRMWARE_STATE_DIR", "")


class _Stop(Exception):
    """Raised by the signal handler so a sleeping loop stops at once."""


class StateError(Exception):
    """The saved state cannot be used by this firmware."""


def _on_signal(signum, frame):
    raise _Stop


def load_state() -> tuple[dict, str]:
    """The saved config in schema 2, converting schema 1 if needed. Returns (state, note)."""
    path = Path(STATE_DIR) / "config.json" if STATE_DIR else None
    if path is None or not path.exists():
        return ({"state_schema": STATE_SCHEMA, "boot_count": 0,
                 "settings": {"interval_seconds": TICK_SECONDS, "units": "metric"}}, "new state")
    try:
        state = json.loads(path.read_text())
    except ValueError as exc:
        raise StateError(f"config.json is not valid JSON: {exc}") from exc
    schema = state.get("state_schema") if isinstance(state, dict) else None
    if type(schema) is not int or schema > STATE_SCHEMA:
        raise StateError(f"cannot read state schema {schema!r} "
                         f"(firmware {VERSION} understands up to schema {STATE_SCHEMA})")
    if schema == 1:
        state["settings"] = {"interval_seconds": state.pop("interval_seconds", TICK_SECONDS),
                             "units": "metric"}
        state["state_schema"] = 2
        return state, "converted state from schema 1 to 2"
    return state, "state schema 2"


def save_state(state: dict) -> None:
    if not STATE_DIR:
        return
    folder = Path(STATE_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / "config.json.tmp"
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, folder / "config.json")


def emit(status: str, errors: int, boot_count: int) -> None:
    print(json.dumps({"version": VERSION, "status": status, "errors": errors,
                      "state_schema": STATE_SCHEMA, "boot_count": boot_count}), flush=True)


def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    try:
        state, note = load_state()
        state["boot_count"] = int(state.get("boot_count", 0)) + 1
        save_state(state)
    except (StateError, OSError, ValueError) as exc:
        print(f"firmware {VERSION} fatal: {exc}", file=sys.stderr, flush=True)
        return 2
    started = time.monotonic()
    print(f"firmware {VERSION} started ({note}, boot {state['boot_count']})",
          file=sys.stderr, flush=True)
    try:
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= FAULT_START_SECONDS + FAULT_CRASH_AFTER_SECONDS:
                print(f"firmware {VERSION} fatal: unrecoverable fault, exiting",
                      file=sys.stderr, flush=True)
                return 1
            if elapsed >= FAULT_START_SECONDS:
                emit("unhealthy", FAULT_ERRORS_PER_TICK, state["boot_count"])
            else:
                emit("healthy", 0, state["boot_count"])
            time.sleep(TICK_SECONDS)
    except _Stop:
        print(f"firmware {VERSION} stopping", file=sys.stderr, flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
