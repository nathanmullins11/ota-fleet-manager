"""OTA demo firmware 2.0: the healthy update.

Same behavior as 1.0, plus an `uptime_seconds` field in each status line so the
upgrade is visible. Receivers ignore fields they do not know.

State: 2.0 keeps its settings in schema 2 (a `settings` block with a new `units` field).
A schema 1 config from 1.0 is converted on first start and saved as schema 2, which 1.0
can no longer read; the agent's pre-update snapshot is what makes rollback safe.
Unknown fields are kept. The interface and the state policy are in firmware/README.md.
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

VERSION = "2.0"
STATE_SCHEMA = 2
TICK_SECONDS = float(os.environ.get("FIRMWARE_TICK_SECONDS", "1"))
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


def emit(status: str, errors: int, uptime: float, boot_count: int) -> None:
    print(json.dumps({"version": VERSION, "status": status, "errors": errors,
                      "uptime_seconds": round(uptime), "state_schema": STATE_SCHEMA,
                      "boot_count": boot_count}), flush=True)


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
    print(f"firmware {VERSION} started ({note}, boot {state['boot_count']})",
          file=sys.stderr, flush=True)
    started = time.monotonic()
    try:
        while True:
            emit("healthy", 0, time.monotonic() - started, state["boot_count"])
            time.sleep(TICK_SECONDS)
    except _Stop:
        print(f"firmware {VERSION} stopping", file=sys.stderr, flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
