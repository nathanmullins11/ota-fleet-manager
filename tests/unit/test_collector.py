"""Control-plane stats collector (deploy/stats-collector/collector.py)."""
import pytest

pytestmark = pytest.mark.unit

MiB = 1024 * 1024


def stats(total, system, usage_mb, cache_mb=0, cpus=2):
    return {"cpu_stats": {"cpu_usage": {"total_usage": total}, "system_cpu_usage": system,
                          "online_cpus": cpus},
            "memory_stats": {"usage": usage_mb * MiB, "stats": {"inactive_file": cache_mb * MiB}}}


def test_memory_excludes_file_cache(collector):
    assert collector.memory_mb(stats(0, 0, 70, cache_mb=10)) == 60.0


def test_cpu_percent_like_docker_stats(collector):
    before = collector.cpu_counters(stats(1_000, 100_000, 0))
    after = collector.cpu_counters(stats(6_000, 200_000, 0))     # 5 % of the host x 2 CPUs
    assert collector.cpu_percent(before, after) == 10.0
    assert collector.cpu_percent(None, after) is None


def test_sample_reads_the_project_containers(collector):
    calls, counter = [], {"n": 0}
    responses = {
        "c1": [stats(1_000, 100_000, 40), stats(3_000, 200_000, 41)],
        "c2": [stats(5_000, 100_000, 60), stats(5_000, 200_000, 60)],
    }

    def get(path):
        calls.append(path)
        if path.startswith("/containers/json"):
            return [{"Id": "c1", "Names": ["/x-orchestrator-1"],
                     "Labels": {"com.docker.compose.service": "orchestrator"}},
                    {"Id": "c2", "Names": ["/x-postgres-1"],
                     "Labels": {"com.docker.compose.service": "postgres"}}]
        cid = path.split("/")[2]
        counter["n"] += 1
        return responses[cid].pop(0)

    c = collector.Collector(get, "ota-demo")
    assert c.sample() == []                                    # first sample: no CPU figure yet
    assert c.sample() == [("orchestrator", 4.0, 41.0), ("postgres", 0.0, 60.0)]
    assert "com.docker.compose.project%3Dota-demo" in calls[0]
