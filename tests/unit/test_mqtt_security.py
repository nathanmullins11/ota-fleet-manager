"""Broker security setup (tools/mqtt_security.py): password hashes, device passwords and the
TLS certificates (FR-7, NF-3)."""
import base64
import hashlib
import hmac
import subprocess

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes

pytestmark = pytest.mark.unit

ENV = {"DEVICE_SECRET": "fleet-secret", "MQTT_ORCHESTRATOR_PASSWORD": "orch-pw",
       "MQTT_OPS_PASSWORD": "ops-pw"}

# Written by the real mosquitto_passwd (Mosquitto 2) for user "tester", password "s3cret".
MOSQUITTO_PASSWD_LINE = (
    "tester:$7$1000$zbGS107VI3kTEmdqEaRbrte995jVqk3dXtAs7cZJ+aPzwNFOQ42KkM/at2PxYk8E6x4WjLUC9QUWNsN"
    "edM+bcA==$cN/mwfB/bnCWxkq93AcBdQhI+eYmvGFWOL+7EI1QyROyBjQt4Ez35jS5zzVPfXUrK1UIaSgypxVggqNAHMYG"
    "bg==")


@pytest.fixture(autouse=True)
def no_secrets_from_environment(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def test_hash_matches_mosquitto_passwd(mqtt_security):
    _, hashed = MOSQUITTO_PASSWD_LINE.split(":", 1)
    salt = base64.b64decode(hashed.split("$")[3])
    assert mqtt_security.mosquitto_hash("s3cret", salt=salt) == hashed


def test_hash_uses_a_fresh_salt_each_time(mqtt_security):
    first, second = mqtt_security.mosquitto_hash("pw"), mqtt_security.mosquitto_hash("pw")
    assert first != second
    assert first.startswith("$7$1000$")
    assert len(base64.b64decode(first.split("$")[3])) == 64


def test_device_password_is_hmac_of_the_device_id(mqtt_security):
    expected = hmac.new(b"fleet-secret", b"device-007", hashlib.sha256).hexdigest()[:32]
    assert mqtt_security.device_password("fleet-secret", "device-007") == expected
    assert mqtt_security.device_password("fleet-secret", "device-008") != expected
    with pytest.raises(mqtt_security.SetupError):
        mqtt_security.device_password("", "device-007")


def test_device_password_matches_the_shell_launchers(mqtt_security):
    """demo/lib/common.sh and fleet.sh derive the same password with openssl."""
    try:
        out = subprocess.run(["openssl", "dgst", "-sha256", "-hmac", "fleet-secret", "-r"],
                             input=b"device-007", capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("openssl is not available")
    assert out.decode().split()[0][:32] == mqtt_security.device_password("fleet-secret", "device-007")


def test_password_file_lists_every_account(mqtt_security):
    lines = mqtt_security.password_file("fleet-secret", "orch-pw", "ops-pw", 3).splitlines()
    users = [line.split(":", 1)[0] for line in lines]
    assert users == ["orchestrator", "ops", "device-001", "device-002", "device-003"]
    device_hash = dict(line.split(":", 1) for line in lines)["device-002"]
    salt = base64.b64decode(device_hash.split("$")[3])
    password = mqtt_security.device_password("fleet-secret", "device-002")
    assert mqtt_security.mosquitto_hash(password, salt=salt) == device_hash


def _load(path):
    return x509.load_pem_x509_certificate(path.read_bytes())


def test_setup_writes_a_ca_signed_broker_certificate(mqtt_security, tmp_path):
    done = mqtt_security.setup(tmp_path, ["203.0.113.10"], 5, ENV)
    assert any("new certificate authority" in line for line in done)
    ca, broker = _load(tmp_path / "ca.crt"), _load(tmp_path / "broker.crt")
    # The broker certificate is signed by the CA (raises if not).
    ca.public_key().verify(broker.signature, broker.tbs_certificate_bytes,
                           ec.ECDSA(broker.signature_hash_algorithm))
    assert broker.issuer == ca.subject
    assert mqtt_security.cert_hosts(broker) == {"mosquitto", "localhost", "host.docker.internal",
                                                "127.0.0.1", "203.0.113.10"}
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert not broker.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert isinstance(broker.signature_hash_algorithm, hashes.SHA256)

    assert (tmp_path / "ca.key").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "device.env").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "device.env").read_text() == "DEVICE_SECRET=fleet-secret\nMQTT_OPS_PASSWORD=ops-pw\n"
    assert len((tmp_path / "passwords").read_text().splitlines()) == 2 + 5


def test_setup_keeps_certificates_clients_already_trust(mqtt_security, tmp_path):
    mqtt_security.setup(tmp_path, [], 2, ENV)
    ca, broker = (tmp_path / "ca.crt").read_bytes(), (tmp_path / "broker.crt").read_bytes()
    done = mqtt_security.setup(tmp_path, ["localhost"], 2, ENV)     # already in the certificate
    assert (tmp_path / "ca.crt").read_bytes() == ca
    assert (tmp_path / "broker.crt").read_bytes() == broker
    assert not any("certificate" in line for line in done)


def test_setup_reissues_only_the_broker_certificate_for_a_new_host(mqtt_security, tmp_path):
    mqtt_security.setup(tmp_path, [], 2, ENV)
    ca, broker = (tmp_path / "ca.crt").read_bytes(), (tmp_path / "broker.crt").read_bytes()
    done = mqtt_security.setup(tmp_path, ["198.51.100.7"], 2, ENV)
    assert (tmp_path / "ca.crt").read_bytes() == ca
    assert (tmp_path / "broker.crt").read_bytes() != broker
    assert "198.51.100.7" in mqtt_security.cert_hosts(_load(tmp_path / "broker.crt"))
    assert any("issued the broker certificate" in line for line in done)


def test_setup_refuses_missing_secrets(mqtt_security, tmp_path):
    with pytest.raises(mqtt_security.SetupError, match="MQTT_OPS_PASSWORD"):
        mqtt_security.setup(tmp_path, [], 2, {"DEVICE_SECRET": "s", "MQTT_ORCHESTRATOR_PASSWORD": "o"})


def test_environment_wins_over_the_env_file(mqtt_security, monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nDEVICE_SECRET = from-file\nOTHER=x\n")
    values = mqtt_security.read_env_file(env_file)
    assert mqtt_security.secret("DEVICE_SECRET", values) == "from-file"
    monkeypatch.setenv("DEVICE_SECRET", "from-env")
    assert mqtt_security.secret("DEVICE_SECRET", values) == "from-env"
    assert mqtt_security.read_env_file(tmp_path / "missing") == {}
