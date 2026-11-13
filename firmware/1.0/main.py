"""OTA demo firmware 1.0: the healthy baseline every device starts on.

The device agent runs this as a child process (`python main.py`) and reads one JSON
status line per tick from stdout. SIGTERM stops it cleanly with exit code 0.

State: the device's settings are kept in FIRMWARE_STATE_DIR/config.json, stamped with
the state schema that wrote them. 1.0 understands schema 1 only: a config written by
newer firmware (schema 2) is refused and the firmware exits with code 2, which is why the
agent restores the pre-update state snapshot on rollback. Unknown fields are kept.
The interface and the state policy are described in firmware/README.md.
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

VERSION = "1.0"
STATE_SCHEMA = 1
TICK_SECONDS = float(os.environ.get("FIRMWARE_TICK_SECONDS", "1"))
STATE_DIR = os.environ.get("FIRMWARE_STATE_DIR", "")


class _Stop(Exception):
    """Raised by the signal handler so a sleeping loop stops at once."""


class StateError(Exception):
    """The saved state cannot be used by this firmware."""


def _on_signal(signum, frame):
    raise _Stop


def load_state() -> dict:
    """The saved config, or a new schema 1 config. Refuses schemas newer than 1."""
    path = Path(STATE_DIR) / "config.json" if STATE_DIR else None
    if path is None or not path.exists():
        return {"state_schema": STATE_SCHEMA, "interval_seconds": TICK_SECONDS, "boot_count": 0}
    try:
        state = json.loads(path.read_text())
    except ValueError as exc:
        raise StateError(f"config.json is not valid JSON: {exc}") from exc
    schema = state.get("state_schema") if isinstance(state, dict) else None
    if type(schema) is not int or schema > STATE_SCHEMA:
        raise StateError(f"cannot read state schema {schema!r} "
                         f"(firmware {VERSION} understands schema {STATE_SCHEMA})")
    return state


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
        state = load_state()
        state["boot_count"] = int(state.get("boot_count", 0)) + 1
        save_state(state)
    except (StateError, OSError, ValueError) as exc:
        print(f"firmware {VERSION} fatal: {exc}", file=sys.stderr, flush=True)
        return 2
    where = STATE_DIR or "memory only (FIRMWARE_STATE_DIR not set)"
    print(f"firmware {VERSION} started (state schema {STATE_SCHEMA}, boot {state['boot_count']}, "
          f"state in {where})", file=sys.stderr, flush=True)
    try:
        while True:
            emit("healthy", 0, state["boot_count"])
            time.sleep(TICK_SECONDS)
    except _Stop:
        print(f"firmware {VERSION} stopping", file=sys.stderr, flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
