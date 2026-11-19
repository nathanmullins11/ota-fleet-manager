"""Control-plane resource collector: CPU and memory of each control-plane container.

Every few seconds it asks the Docker Engine (through its socket, read-only use) for the
stats of the containers in its own Compose project, and writes one row per service to
control_plane_stats for the dashboard. Device resource use is not collected here: devices
report their own in their heartbeats, because in the cloud setup they run on another machine.

CPU is computed like `docker stats`: CPU time used since the previous sample divided by
the host's CPU time in the same period, times the number of CPUs, so 100 % is one core.
Memory is usage minus reclaimable file cache, also like `docker stats`.
"""
import http.client
import json
import logging
import os
import socket
import time
import urllib.parse
from typing import Callable, Optional

import psycopg

log = logging.getLogger("stats-collector")

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 10):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def docker_api(socket_path: str) -> Callable[[str], object]:
    """A GET function for the Docker Engine API over its Unix socket."""
    def get(path: str):
        conn = _UnixHTTPConnection(socket_path)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                raise RuntimeError(f"Docker API {path} returned HTTP {resp.status}")
            return json.loads(body)
        finally:
            conn.close()
    return get


def memory_mb(stats: dict) -> float:
    mem = stats.get("memory_stats") or {}
    detail = mem.get("stats") or {}
    cache = detail.get("inactive_file", detail.get("total_inactive_file", 0))
    return round(max(mem.get("usage", 0) - cache, 0) / (1024 * 1024), 1)


def cpu_counters(stats: dict) -> Optional[tuple[int, int, int]]:
    """(container CPU time, host CPU time, number of CPUs), or None if missing."""
    cpu = stats.get("cpu_stats") or {}
    total = (cpu.get("cpu_usage") or {}).get("total_usage")
    system = cpu.get("system_cpu_usage")
    cpus = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
    if total is None or system is None:
        return None
    return total, system, cpus


def cpu_percent(previous: Optional[tuple], current: Optional[tuple]) -> Optional[float]:
    if previous is None or current is None:
        return None
    container = current[0] - previous[0]
    host = current[1] - previous[1]
    if host <= 0 or container < 0:
        return None
    return round(container / host * current[2] * 100, 2)


class Collector:
    def __init__(self, get: Callable[[str], object], project: str):
        self.get = get
        self.project = project
        self._previous: dict = {}         # container id -> cpu_counters

    def sample(self) -> list[tuple[str, float, float]]:
        """(service, cpu_percent, memory_mb) for each running container of the project.
        A container's first sample has no CPU figure yet and is skipped."""
        filters = urllib.parse.quote(json.dumps({"label": [f"{PROJECT_LABEL}={self.project}"]}))
        rows, seen = [], set()
        for container in self.get(f"/containers/json?filters={filters}"):
            cid = container["Id"]
            seen.add(cid)
            service = (container.get("Labels") or {}).get(SERVICE_LABEL, container["Names"][0])
            stats = self.get(f"/containers/{cid}/stats?stream=false&one-shot=true")
            counters = cpu_counters(stats)
            cpu = cpu_percent(self._previous.get(cid), counters)
            self._previous[cid] = counters
            if cpu is not None:
                rows.append((service, cpu, memory_mb(stats)))
        self._previous = {k: v for k, v in self._previous.items() if k in seen}
        return sorted(rows)


def own_project(get: Callable[[str], object]) -> str:
    """The Compose project this collector belongs to, from its own container's labels."""
    me = get(f"/containers/{socket.gethostname()}/json")
    return me["Config"]["Labels"][PROJECT_LABEL]


def connect_db():
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "postgres"), port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ.get("POSTGRES_DB", "ota"), user=os.environ.get("POSTGRES_USER", "ota"),
        password=os.environ["POSTGRES_PASSWORD"], autocommit=True, connect_timeout=5)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    interval = float(os.environ.get("STATS_INTERVAL_SECONDS", "5"))
    keep_hours = int(os.environ.get("STATS_RETENTION_HOURS", "24"))
    get = docker_api(os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock"))
    project = own_project(get)
    log.info("Collecting CPU and memory of Compose project %r every %gs", project, interval)
    collector, conn, next_cleanup = Collector(get, project), None, 0.0
    while True:
        started = time.monotonic()
        try:
            rows = collector.sample()
            if conn is None or conn.closed:
                conn = connect_db()
            with conn.transaction():
                for service, cpu, mem in rows:
                    conn.execute("INSERT INTO control_plane_stats (service, cpu_percent, memory_mb) "
                                 "VALUES (%s, %s, %s)", (service, cpu, mem))
                if started >= next_cleanup:
                    conn.execute("DELETE FROM control_plane_stats "
                                 "WHERE sampled_at < now() - make_interval(hours => %s)", (keep_hours,))
                    next_cleanup = started + 3600
            log.debug("Recorded %d services", len(rows))
        except Exception as exc:
            log.warning("Sample failed (%s); retrying", exc)
            if conn is not None:
                conn.close()
                conn = None
        time.sleep(max(interval - (time.monotonic() - started), 0.5))


if __name__ == "__main__":
    main()
