"""Device agent configuration and its agreement with the orchestrator's rules."""
import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def env(monkeypatch):
    for key in ("DEVICE_ID", "HEARTBEAT_INTERVAL_SECONDS", "MQTT_PORT", "FIRMWARE_DIR",
                "FIRMWARE_START_TIMEOUT_SECONDS", "MQTT_USERNAME", "MQTT_PASSWORD", "MQTT_CA_CERT"):
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



def test_broker_sign_in_defaults_to_the_device_id(agent_config, env, tmp_path):
    env(DEVICE_ID="device-004", FIRMWARE_DIR=str(tmp_path), MQTT_PASSWORD="pw",
        MQTT_CA_CERT="/run/ota/ca.crt")
    cfg = agent_config.load_config()
    assert (cfg.mqtt_username, cfg.mqtt_password, cfg.mqtt_ca_cert) == (
        "device-004", "pw", "/run/ota/ca.crt")


def test_agent_client_uses_tls_and_its_own_account(agent, agent_config, env, tmp_path,
                                                   monkeypatch):
    env(DEVICE_ID="device-004", FIRMWARE_DIR=str(tmp_path), MQTT_PASSWORD="pw",
        MQTT_CA_CERT="/run/ota/ca.crt")
    calls = {}
    monkeypatch.setattr(agent.mqtt.Client, "username_pw_set",
                        lambda self, *a: calls.__setitem__("sign_in", a))
    monkeypatch.setattr(agent.mqtt.Client, "tls_set",
                        lambda self, **kw: calls.__setitem__("tls", kw))
    agent.build_client(agent_config.load_config(), None, None)
    assert calls == {"sign_in": ("device-004", "pw"), "tls": {"ca_certs": "/run/ota/ca.crt"}}


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
