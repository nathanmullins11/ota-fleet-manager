"""OTA demo firmware 2.1: the faulty update.

It looks fine at first, then fails the same way every run, so experiments are
repeatable:

  0s  to 10s   healthy, 0 errors per tick
  10s to 15s   unhealthy, 5 errors per tick
  15s          crashes (exit code 1)

The agent restarts it after a crash, so the cycle repeats until the device is rolled
back. The timings can be shortened for tests with the FIRMWARE_FAULT_* variables.
The interface is described in firmware/README.md.
"""
import json
import os
import signal
import sys
import time

VERSION = "2.1"
TICK_SECONDS = float(os.environ.get("FIRMWARE_TICK_SECONDS", "1"))
FAULT_START_SECONDS = float(os.environ.get("FIRMWARE_FAULT_START_SECONDS", "10"))
FAULT_CRASH_AFTER_SECONDS = float(os.environ.get("FIRMWARE_FAULT_CRASH_AFTER_SECONDS", "5"))
FAULT_ERRORS_PER_TICK = int(os.environ.get("FIRMWARE_FAULT_ERRORS_PER_TICK", "5"))


class _Stop(Exception):
    """Raised by the signal handler so a sleeping loop stops at once."""


def _on_signal(signum, frame):
    raise _Stop


def emit(status: str, errors: int) -> None:
    print(json.dumps({"version": VERSION, "status": status, "errors": errors}), flush=True)


def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    started = time.monotonic()
    print(f"firmware {VERSION} started", file=sys.stderr, flush=True)
    try:
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= FAULT_START_SECONDS + FAULT_CRASH_AFTER_SECONDS:
                print(f"firmware {VERSION} fatal: unrecoverable fault, exiting",
                      file=sys.stderr, flush=True)
                return 1
            if elapsed >= FAULT_START_SECONDS:
                emit("unhealthy", FAULT_ERRORS_PER_TICK)
            else:
                emit("healthy", 0)
            time.sleep(TICK_SECONDS)
    except _Stop:
        print(f"firmware {VERSION} stopping", file=sys.stderr, flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
