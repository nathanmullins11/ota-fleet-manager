"""Device agent configuration and its agreement with the orchestrator's rules."""
import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def env(monkeypatch):
    for key in ("DEVICE_ID", "FIRMWARE_VERSION", "HEARTBEAT_INTERVAL_SECONDS", "MQTT_PORT"):
        monkeypatch.delenv(key, raising=False)

    def set_env(**values):
        for key, value in values.items():
            monkeypatch.setenv(key, value)
    return set_env


def test_valid_config(agent_config, env):
    env(DEVICE_ID="device-001", FIRMWARE_VERSION="2.1.3")
    cfg = agent_config.load_config()
    assert (cfg.device_id, cfg.firmware_version) == ("device-001", "2.1.3")


@pytest.mark.parametrize("device_id", ["device-001\n", "device-١٢٣", "device-01", ""])
def test_bad_device_id_exits(agent_config, env, device_id):
    env(DEVICE_ID=device_id)
    with pytest.raises(SystemExit, match="DEVICE_ID"):
        agent_config.load_config()


@pytest.mark.parametrize("version", ["1.0\n", "١.٠", "1", "v1.0"])
def test_bad_firmware_version_exits(agent_config, env, version):
    env(DEVICE_ID="device-001", FIRMWARE_VERSION=version)
    with pytest.raises(SystemExit, match="FIRMWARE_VERSION"):
        agent_config.load_config()


@pytest.mark.parametrize("value", ["device-001", "device-12345", "device-001\n", "device-١٢٣",
                                   "device-01", "x"])
def test_agent_and_orchestrator_agree_on_device_ids(agent_config, topics, value):
    """The two services must accept exactly the same IDs, or a device could start but never register."""
    assert bool(agent_config.DEVICE_ID_RE.fullmatch(value)) == \
        bool(topics.DEVICE_ID_RE.fullmatch(value))


@pytest.mark.parametrize("value", ["1.0", "2.1.3", "1.0\n", "١.٠", "1", "1.0.0.0"])
def test_agent_and_orchestrator_agree_on_versions(agent_config, messages, value):
    assert bool(agent_config.VERSION_RE.fullmatch(value)) == \
        bool(messages.VERSION_RE.fullmatch(value))
