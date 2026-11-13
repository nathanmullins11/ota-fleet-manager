"""Device agent configuration and its agreement with the orchestrator's rules."""
import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def env(monkeypatch):
    for key in ("DEVICE_ID", "HEARTBEAT_INTERVAL_SECONDS", "MQTT_PORT", "FIRMWARE_DIR",
                "FIRMWARE_START_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key, raising=False)

    def set_env(**values):
        for key, value in values.items():
            monkeypatch.setenv(key, value)
    return set_env


def test_valid_config(agent_config, env, tmp_path):
    env(DEVICE_ID="device-001", FIRMWARE_DIR=str(tmp_path))
    cfg = agent_config.load_config()
    assert (cfg.device_id, cfg.firmware_dir) == ("device-001", tmp_path)
    assert (cfg.factory_dir / "1.0" / "manifest.json").is_file()
    assert cfg.public_key_path.is_file()


@pytest.mark.parametrize("device_id", ["device-001\n", "device-١٢٣", "device-01", ""])
def test_bad_device_id_exits(agent_config, env, device_id):
    env(DEVICE_ID=device_id)
    with pytest.raises(SystemExit, match="DEVICE_ID"):
        agent_config.load_config()


@pytest.mark.parametrize("key,value", [("HEARTBEAT_INTERVAL_SECONDS", "0"),
                                       ("HEARTBEAT_INTERVAL_SECONDS", "x"),
                                       ("FIRMWARE_START_TIMEOUT_SECONDS", "-1")])
def test_bad_timing_setting_exits(agent_config, env, key, value):
    env(DEVICE_ID="device-001", **{key: value})
    with pytest.raises(SystemExit, match=key):
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
