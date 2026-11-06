"""OTA demo firmware 2.0: the healthy update.

Same behavior as 1.0, plus an `uptime_seconds` field in each status line so the
upgrade is visible. Receivers ignore fields they do not know, so the agent needs no
change to run it. The interface is described in firmware/README.md.
"""
import json
import os
import signal
import sys
import time

VERSION = "2.0"
TICK_SECONDS = float(os.environ.get("FIRMWARE_TICK_SECONDS", "1"))


class _Stop(Exception):
    """Raised by the signal handler so a sleeping loop stops at once."""


def _on_signal(signum, frame):
    raise _Stop


def emit(status: str, errors: int, uptime: float) -> None:
    print(json.dumps({"version": VERSION, "status": status, "errors": errors,
                      "uptime_seconds": round(uptime)}), flush=True)


def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    started = time.monotonic()
    print(f"firmware {VERSION} started", file=sys.stderr, flush=True)
    try:
        while True:
            emit("healthy", 0, time.monotonic() - started)
            time.sleep(TICK_SECONDS)
    except _Stop:
        print(f"firmware {VERSION} stopping", file=sys.stderr, flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
