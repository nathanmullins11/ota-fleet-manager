"""OTA demo firmware 1.0: the healthy baseline every device starts on.

The device agent runs this as a child process (`python main.py`) and reads one JSON
status line per tick from stdout. SIGTERM stops it cleanly with exit code 0.
The interface is described in firmware/README.md.
"""
import json
import os
import signal
import sys
import time

VERSION = "1.0"
TICK_SECONDS = float(os.environ.get("FIRMWARE_TICK_SECONDS", "1"))


class _Stop(Exception):
    """Raised by the signal handler so a sleeping loop stops at once."""


def _on_signal(signum, frame):
    raise _Stop


def emit(status: str, errors: int) -> None:
    print(json.dumps({"version": VERSION, "status": status, "errors": errors}), flush=True)


def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    print(f"firmware {VERSION} started", file=sys.stderr, flush=True)
    try:
        while True:
            emit("healthy", 0)
            time.sleep(TICK_SECONDS)
    except _Stop:
        print(f"firmware {VERSION} stopping", file=sys.stderr, flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
