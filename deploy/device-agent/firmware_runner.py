"""Runs the installed firmware as a child process and turns its output into health data.

The interface is described in firmware/README.md: the firmware prints one JSON status
line per tick on stdout, logs on stderr, exits 0 when asked to stop (SIGTERM), and any
exit the agent did not ask for is a crash. After a crash the runner waits a moment and
starts the firmware again, so a faulty version keeps cycling until it is replaced.

The heartbeat loop calls take_health() for the status, errors and crashes since the
previous heartbeat. Each process start is a "run"; output from an old run is ignored.
"""
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("device-agent.firmware")

# Worst first wins when summarising a heartbeat interval.
STATUS_RANK = {"healthy": 0, "degraded": 1, "unhealthy": 2}
INT4_MAX = 2**31 - 1    # error_count and crash_count are INTEGER columns
STOP_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class Health:
    status: str             # healthy, degraded or unhealthy
    errors: int             # sum of the firmware's per-tick errors since the last call
    crashes: int            # unexpected exits since the last call


def parse_status_line(line: str) -> Optional[tuple[str, int]]:
    """(status, errors) from one stdout line, or None if it is not a valid status line."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("status") not in STATUS_RANK:
        return None
    errors = data.get("errors", 0)
    if type(errors) is not int or errors < 0:
        errors = 0
    return data["status"], errors


class FirmwareRunner:
    def __init__(self, restart_delay_seconds: float = 2.0):
        self.restart_delay = restart_delay_seconds
        self._cond = threading.Condition()
        self._proc: Optional[subprocess.Popen] = None
        self._run = 0                 # increments on every process start
        self._wanted = False          # True while the firmware should be kept running
        self._version: Optional[str] = None
        self._app_dir: Optional[Path] = None
        self._entrypoint = "main.py"
        self._down = False            # crashed and waiting to be restarted
        self._trial_run = 0           # run on trial: an exit before its first status is not a crash
        # Per-run facts used by wait_until_started().
        self._status_run = 0          # run that printed the latest status line
        self._latest_status: Optional[str] = None
        self._exited_run = 0
        self._exit_code: Optional[int] = None
        # Accumulated for the next heartbeat.
        self._errors = 0
        self._crashes = 0
        self._worst: Optional[str] = None

    # ----------------------------------------------------------------- control

    @property
    def version(self) -> Optional[str]:
        return self._version

    def start(self, version: str, app_dir: Path, entrypoint: str = "main.py",
              trial: bool = False) -> None:
        """Start a version and keep it running (restarting after crashes) until stop().

        trial: a newly installed version. If it exits before reporting its first status,
        that is a failed install, not a crash: it is not counted or restarted, and
        wait_until_started() reports it so the caller can go back to the old version.
        """
        with self._cond:
            if self._wanted:
                raise RuntimeError(f"firmware {self._version} is still running; stop it first")
            self._version, self._app_dir, self._entrypoint = version, Path(app_dir), entrypoint
            self._wanted = True
            self._latest_status = None
            self._trial_run = self._run + 1 if trial else 0     # the run _spawn() starts
            self._spawn()

    def stop(self) -> None:
        """Stop the firmware on purpose (not a crash). Waits until the process has exited."""
        with self._cond:
            self._wanted = False
            self._down = False
            proc = self._proc
            self._cond.notify_all()       # cancels a pending restart
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            log.warning("Firmware %s ignored SIGTERM; killing it", self._version)
            proc.kill()
            proc.wait()

    def wait_until_started(self, timeout: float) -> Optional[str]:
        """Wait for the current run's first status line. Returns None if it reported
        healthy or degraded in time, otherwise the reason it did not start properly."""
        with self._cond:
            run = self._run
            self._cond.wait_for(
                lambda: self._status_run == run or self._exited_run == run, timeout=timeout)
            if self._status_run == run:
                if self._latest_status == "unhealthy":
                    return f"firmware {self._version} reported unhealthy on start"
                return None
            if self._exited_run == run:
                return (f"firmware {self._version} exited with code {self._exit_code} "
                        "before reporting its status")
            return f"firmware {self._version} did not report its status within {timeout:g} s"

    def take_health(self) -> Health:
        """Status, errors and crashes since the previous call; resets the counters."""
        with self._cond:
            if self._down:
                status = "unhealthy"
            else:
                status = self._worst or self._latest_status or "healthy"
            health = Health(status, min(self._errors, INT4_MAX), min(self._crashes, INT4_MAX))
            self._errors = self._crashes = 0
            self._worst = "unhealthy" if self._down else None
            return health

    # ----------------------------------------------------------------- internals

    def _spawn(self) -> None:
        """Start a process for the current version. Called with the lock held."""
        self._run += 1
        run = self._run
        self._down = False
        try:
            proc = subprocess.Popen(
                [sys.executable, self._entrypoint], cwd=self._app_dir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1"})
        except OSError as exc:
            log.error("Could not start firmware %s: %s", self._version, exc)
            self._proc = None
            self._on_exit(run, None)
            return
        self._proc = proc
        log.info("Firmware %s started (pid %d)", self._version, proc.pid)
        stderr_tail: deque = deque(maxlen=5)
        reader = threading.Thread(target=self._read_stdout, args=(proc, run), daemon=True)
        logger = threading.Thread(target=self._read_stderr, args=(proc, stderr_tail), daemon=True)
        reader.start()
        logger.start()
        threading.Thread(target=self._watch, args=(proc, run, reader, stderr_tail),
                         daemon=True).start()

    def _read_stdout(self, proc: subprocess.Popen, run: int) -> None:
        for line in proc.stdout:
            parsed = parse_status_line(line)
            if parsed is None:
                log.debug("Ignoring firmware output: %s", line.rstrip()[:200])
                continue
            status, errors = parsed
            with self._cond:
                if run != self._run:
                    continue
                self._latest_status = status
                self._status_run = run
                self._errors += errors
                if self._worst is None or STATUS_RANK[status] > STATUS_RANK[self._worst]:
                    self._worst = status
                self._cond.notify_all()

    def _read_stderr(self, proc: subprocess.Popen, tail: deque) -> None:
        for line in proc.stderr:
            line = line.rstrip()
            tail.append(line)
            log.info("firmware: %s", line[:300])

    def _watch(self, proc: subprocess.Popen, run: int, reader: threading.Thread,
               stderr_tail: deque) -> None:
        code = proc.wait()
        reader.join(timeout=1)       # count the last status lines before the exit
        with self._cond:
            if run != self._run:
                return
            failed_trial = run == self._trial_run and self._status_run != run
            if not self._wanted or failed_trial:
                log.info("Firmware %s %s (exit code %s)", self._version,
                         "stopped" if not self._wanted else "exited before reporting its status",
                         code)
                self._proc = None
                self._exited_run, self._exit_code = run, code
                self._cond.notify_all()
                return
            last = stderr_tail[-1] if stderr_tail else "no output"
            log.warning("Firmware %s crashed (exit code %s): %s", self._version, code, last)
            self._on_exit(run, code)

    def _on_exit(self, run: int, code: Optional[int]) -> None:
        """Record an unexpected exit and restart after a delay. Called with the lock held."""
        self._proc = None
        self._exited_run, self._exit_code = run, code
        self._crashes += 1
        self._down = True
        self._worst = "unhealthy"
        self._cond.notify_all()
        threading.Thread(target=self._restart_later, args=(run,), daemon=True).start()

    def _restart_later(self, run: int) -> None:
        with self._cond:
            cancelled = self._cond.wait_for(
                lambda: not self._wanted or self._run != run, timeout=self.restart_delay)
            if not cancelled:
                log.info("Restarting firmware %s", self._version)
                self._spawn()
