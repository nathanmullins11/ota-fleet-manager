"""Device agent: measuring its container's CPU and memory (resources.py) from cgroup files."""
import pytest

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


def test_cgroup_v2(agent_resources, tmp_path, clock):
    (tmp_path / "memory.current").write_text(f"{30 * 1024 * 1024}\n")
    (tmp_path / "memory.stat").write_text(f"anon 1\ninactive_file {6 * 1024 * 1024}\n")
    (tmp_path / "cpu.stat").write_text("usage_usec 1000000\nuser_usec 1\n")
    sampler = agent_resources.ResourceSampler(tmp_path, clock)
    assert sampler.sample() == {"memory_mb": 24.0}            # no CPU figure on the first call
    clock.now += 2
    (tmp_path / "cpu.stat").write_text("usage_usec 1100000\n")  # 0.1 s of CPU in 2 s
    assert sampler.sample() == {"cpu_percent": 5.0, "memory_mb": 24.0}


def test_cgroup_v1(agent_resources, tmp_path, clock):
    (tmp_path / "memory").mkdir()
    (tmp_path / "cpuacct").mkdir()
    (tmp_path / "memory" / "memory.usage_in_bytes").write_text(f"{20 * 1024 * 1024}")
    (tmp_path / "memory" / "memory.stat").write_text(f"total_inactive_file {4 * 1024 * 1024}\n")
    (tmp_path / "cpuacct" / "cpuacct.usage").write_text("5000000000")
    sampler = agent_resources.ResourceSampler(tmp_path, clock)
    sampler.sample()
    clock.now += 1
    (tmp_path / "cpuacct" / "cpuacct.usage").write_text("5500000000")   # 0.5 s in 1 s
    assert sampler.sample() == {"cpu_percent": 50.0, "memory_mb": 16.0}


def test_no_cgroup_files_means_no_fields(agent_resources, tmp_path, clock):
    sampler = agent_resources.ResourceSampler(tmp_path / "missing", clock)
    sampler.sample()
    clock.now += 1
    assert sampler.sample() == {}


def test_heartbeat_carries_the_fields(agent):
    hb = agent.build_heartbeat("device-001", "1.0", "healthy", 0, 0,
                               {"cpu_percent": 1.5, "memory_mb": 24.0})
    assert (hb["cpu_percent"], hb["memory_mb"]) == (1.5, 24.0)
    assert "cpu_percent" not in agent.build_heartbeat("device-001", "1.0", "healthy", 0, 0)


def test_agent_heartbeat_is_accepted_by_the_orchestrator(agent, messages):
    import json
    hb = agent.build_heartbeat("device-001", "1.0", "healthy", 0, 0,
                               {"cpu_percent": 0.4, "memory_mb": 23.9})
    parsed = messages.parse_heartbeat("device-001", json.dumps(hb).encode())
    assert (parsed.cpu_percent, parsed.memory_mb) == (0.4, 23.9)
