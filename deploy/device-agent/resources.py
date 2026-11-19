"""The device container's own CPU and memory use, for the heartbeat (optional fields).

Read from the container's cgroup, so it covers the agent and its firmware child process
together, and matches what `docker stats` reports:

  memory_mb    memory in use minus reclaimable file cache, in MiB
  cpu_percent  CPU time used since the previous sample, as a percent of one core

cgroup v2 (current Docker) is read first, then cgroup v1. Outside a container, or if the
files are missing, a field is simply left out of the heartbeat.
"""
import time
from pathlib import Path
from typing import Callable, Optional


def _read_int(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _stat_field(path: Path, key: str) -> Optional[int]:
    try:
        for line in path.read_text().splitlines():
            name, _, value = line.partition(" ")
            if name == key:
                return int(value)
    except (OSError, ValueError):
        pass
    return None


class ResourceSampler:
    def __init__(self, root: Path = Path("/sys/fs/cgroup"),
                 clock: Callable[[], float] = time.monotonic):
        self.root = Path(root)
        self.clock = clock
        self._last: Optional[tuple[float, int]] = None     # (time, CPU microseconds)

    def memory_mb(self) -> Optional[float]:
        v2 = _read_int(self.root / "memory.current")
        if v2 is not None:
            used = v2 - (_stat_field(self.root / "memory.stat", "inactive_file") or 0)
        else:
            v1 = _read_int(self.root / "memory" / "memory.usage_in_bytes")
            if v1 is None:
                return None
            used = v1 - (_stat_field(self.root / "memory" / "memory.stat", "total_inactive_file") or 0)
        return round(max(used, 0) / (1024 * 1024), 1)

    def _cpu_microseconds(self) -> Optional[int]:
        usage = _stat_field(self.root / "cpu.stat", "usage_usec")           # v2
        if usage is not None:
            return usage
        nanoseconds = _read_int(self.root / "cpuacct" / "cpuacct.usage")    # v1
        return None if nanoseconds is None else nanoseconds // 1000

    def cpu_percent(self) -> Optional[float]:
        """CPU use since the previous call; None on the first call (nothing to compare)."""
        usage, now = self._cpu_microseconds(), self.clock()
        if usage is None:
            return None
        last, self._last = self._last, (now, usage)
        if last is None or now <= last[0]:
            return None
        return round(max(usage - last[1], 0) / ((now - last[0]) * 1_000_000) * 100, 2)

    def sample(self) -> dict:
        """The heartbeat fields that could be measured."""
        fields = {"cpu_percent": self.cpu_percent(), "memory_mb": self.memory_mb()}
        return {k: v for k, v in fields.items() if v is not None}
