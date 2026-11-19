"""STRIDE threats checked without running services: parsers, the installer, configuration.

IDs (S1, T2, ...) refer to the threat table in docs/threat-model.md. Tests marked
open_gap(...) describe the secure behaviour for a threat that is still open.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

from .stride import open_gap

pytestmark = [pytest.mark.unit, pytest.mark.security]

TS = "2026-11-16T10:00:00Z"
REPO_ROOT = Path(__file__).resolve().parents[2]


def mosquitto_settings(text: str) -> dict:
    """mosquitto.conf as {option: [values]}, comments and blank lines dropped."""
    settings: dict = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            key, _, value = line.partition(" ")
            settings.setdefault(key, []).append(value.strip())
    return settings


# =========================================================================== S: Spoofing

@open_gap("S1", "the broker accepts anonymous clients until Iteration 3 (FR-7, NF-3)")
def test_S1_broker_rejects_anonymous_clients(read_repo):
    conf = mosquitto_settings(read_repo("deploy/mqtt/mosquitto.conf"))
    assert conf.get("allow_anonymous") == ["false"]
    assert "password_file" in conf or "require_certificate" in conf


@open_gap("S2", "no ACL, so any client can publish heartbeats, events or commands for any device")
def test_S2_broker_limits_each_client_to_its_own_topics(read_repo):
    conf = mosquitto_settings(read_repo("deploy/mqtt/mosquitto.conf"))
    assert "acl_file" in conf


def test_S3_heartbeat_for_another_device_is_rejected(messages):
    """A device cannot report on behalf of another one through its own topic."""
    payload = json.dumps({"schema_version": 1, "device_id": "device-002", "version": "1.0",
                          "status": "healthy", "error_count": 0, "timestamp": TS}).encode()
    with pytest.raises(messages.InvalidMessage, match="does not match topic"):
        messages.parse_heartbeat("device-001", payload)


def test_S3_event_for_another_device_is_rejected(messages):
    payload = json.dumps({"schema_version": 1, "device_id": "device-002",
                          "event": "update_failed", "rollout_id": 1, "version": "2.0",
                          "reason": "x", "timestamp": TS}).encode()
    with pytest.raises(messages.InvalidMessage, match="does not match topic"):
        messages.parse_event("device-001", payload)


def test_S3_device_subscribes_only_to_its_own_commands(agent):
    assert agent.command_topic("device-001") == "ota/devices/device-001/commands"
    assert "+" not in agent.command_topic("device-001") and "#" not in agent.command_topic("device-001")


# =========================================================================== T: Tampering

def command_payload(**overrides):
    cmd = {"schema_version": 1, "command": "update", "rollout_id": 7, "target_version": "2.0",
           "download_url": "http://registry:8080/packages/2.0",
           "manifest_url": "http://registry:8080/packages/2.0/manifest",
           "signature_url": "http://registry:8080/packages/2.0/manifest.sig",
           "sha256": "a" * 64, "issued_at": TS}
    cmd.update(overrides)
    return json.dumps(cmd).encode()


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",                 # local file read through urllib
    "ftp://registry/packages/2.0",
    "javascript:alert(1)",
    "//registry/packages/2.0",            # scheme-relative
    "http:///packages/2.0",               # no host
    "registry:8080/packages/2.0",
])
@pytest.mark.parametrize("key", ["download_url", "manifest_url", "signature_url"])
def test_T1_command_urls_must_be_http(agent_commands, key, url):
    with pytest.raises(agent_commands.InvalidCommand, match=f"{key} must be an absolute http"):
        agent_commands.parse_command(command_payload(**{key: url}))


@pytest.mark.parametrize("sha", ["A" * 64, "a" * 63, "g" * 64, 123, None])
def test_T1_command_checksum_must_be_well_formed(agent_commands, sha):
    with pytest.raises(agent_commands.InvalidCommand, match="sha256"):
        agent_commands.parse_command(command_payload(sha256=sha))


@pytest.mark.parametrize("version", ["../1.0", "1.0/../../etc", "1.0\n", "1.0 "])
def test_T1_command_version_cannot_name_a_path(agent_commands, version):
    """The version becomes a folder name on the device."""
    with pytest.raises(agent_commands.InvalidCommand, match="target_version"):
        agent_commands.parse_command(command_payload(target_version=version))


MAIN = 'VERSION = "2.0"\n'


@pytest.mark.parametrize("member", [
    ("../escaped.txt", "file", "outside"),
    ("app/../../escaped.txt", "file", "outside"),
    ("link", "symlink", "/etc/passwd"),
    ("link", "symlink", "../../../../outside"),
    ("hard", "hardlink", "/etc/passwd"),
])
def test_T2_signed_package_cannot_write_outside_its_folder(installer, signing_key, make_package,
                                                         tmp_path, member):
    """Even a correctly signed package (stolen key, compromised build) is unpacked safely."""
    store = installer.Store(tmp_path / "fw", signing_key.public_key())
    manifest, sig, package = make_package("2.0", [("main.py", "file", MAIN), member])
    with pytest.raises(installer.InstallError, match="could not unpack"):
        store.add("2.0", installer.Download(manifest, sig, package))
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path.parent / "escaped.txt").exists()
    assert not (store.versions_dir / "2.0").exists()


def test_T2_absolute_path_in_package_is_unpacked_inside_its_folder(installer, signing_key,
                                                                   make_package, tmp_path):
    """tarfile's "data" filter strips the leading slash instead of writing to /tmp."""
    target = tmp_path / "absolute-escape.txt"
    store = installer.Store(tmp_path / "fw", signing_key.public_key())
    manifest, sig, package = make_package("2.0", [("main.py", "file", MAIN),
                                                  (str(target), "file", "outside")])
    installed = store.add("2.0", installer.Download(manifest, sig, package))
    assert not target.exists()
    assert (installed.app_dir / str(target).lstrip("/")).read_text() == "outside"


@pytest.mark.parametrize("entrypoint", ["../agent.py", "/usr/bin/python3", "sub/main.py", "..", ""])
def test_T2_manifest_entrypoint_must_be_a_plain_file_name(installer, signing_key, make_package,
                                                        tmp_path, entrypoint):
    store = installer.Store(tmp_path / "fw", signing_key.public_key())
    manifest, sig, package = make_package("2.0", [("main.py", "file", MAIN)], entrypoint=entrypoint)
    with pytest.raises(installer.InstallError, match="entrypoint"):
        store.add("2.0", installer.Download(manifest, sig, package))


def test_T2_package_signed_by_another_key_is_refused(installer, signing_key, make_package,
                                                    release, tmp_path):
    attacker_key = release.Ed25519PrivateKey.generate()
    store = installer.Store(tmp_path / "fw", signing_key.public_key())
    manifest, sig, package = make_package("2.0", [("main.py", "file", MAIN)], key=attacker_key)
    with pytest.raises(installer.VerificationError, match="signature verification failed"):
        store.add("2.0", installer.Download(manifest, sig, package))


def test_T2_device_code_and_release_key_are_read_only_for_the_agent(read_repo):
    """Copied in as root before USER: neither the agent nor its firmware can replace them."""
    dockerfile = read_repo("deploy/device-agent/Dockerfile")
    lines = [line.strip() for line in dockerfile.splitlines()]
    copy_key = next(i for i, line in enumerate(lines)
                    if line.startswith("COPY") and "release-public.pem" in line)
    user = next(i for i, line in enumerate(lines) if line.startswith("USER"))
    assert copy_key < user
    assert "--chown" not in lines[copy_key]


def test_T2_release_public_key_matches_the_signing_key(read_repo):
    """Devices trust exactly the key releases are verified with."""
    assert read_repo("deploy/device-agent/release-public.pem") == read_repo("keys/release-public.pem")


# =========================================================================== R: Repudiation

def columns(sql: str, table: str) -> set:
    body = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.S).group(1)
    # Column lines are indented four spaces: "    name  TYPE ..."; constraints sit deeper.
    return set(re.findall(r"^ {4}([a-z_]+)\s+[A-Z]", body, re.M))


def test_R1_every_rollout_change_is_logged_with_a_time(read_repo):
    cols = columns(read_repo("deploy/registries/postgres/init.sql"), "rollout_events")
    assert {"rollout_id", "event", "reason", "created_at"} <= cols


@open_gap("R2", "rollout events record what happened but not which operator asked for it")
def test_R2_operator_actions_record_who_did_them(read_repo):
    cols = columns(read_repo("deploy/registries/postgres/init.sql"), "rollout_events")
    assert cols & {"actor", "operator", "requested_by"}


# =========================================================================== I: Information disclosure

def tracked_files():
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True,
                             check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [REPO_ROOT / name for name in out.decode().split("\0") if name]



def test_I1_no_private_key_is_committed():
    leaks = []
    for path in tracked_files():
        try:
            if b"PRIVATE KEY-----" in path.read_bytes():
                leaks.append(str(path.relative_to(REPO_ROOT)))
        except OSError:
            continue
    assert leaks == []


def test_I1_secrets_are_ignored_by_git(read_repo):
    ignored = {line.strip() for line in read_repo(".gitignore").splitlines()}
    assert {".env", "keys/release-private.pem"} <= ignored


def test_I2_database_is_reachable_only_from_the_host(read_repo):
    compose = read_repo("deploy/docker-compose.yml")
    published = re.findall(r'-\s*"([^"]*:5432)"', compose)
    assert published == ["127.0.0.1:5432:5432"]


@open_gap("I3", "MQTT traffic (commands, heartbeats, events) is plain text until Iteration 3")
def test_I3_broker_listens_with_tls_only(read_repo):
    conf = mosquitto_settings(read_repo("deploy/mqtt/mosquitto.conf"))
    assert "certfile" in conf and "keyfile" in conf
    assert "1883" not in conf.get("listener", [])


@open_gap("I4", "firmware inherits the agent's whole environment, including future broker credentials")
def test_I4_firmware_does_not_inherit_agent_secrets(firmware_runner, monkeypatch, tmp_path):
    monkeypatch.setenv("MQTT_PASSWORD", "device-secret")
    monkeypatch.setenv("MQTT_USERNAME", "device-001")
    env = firmware_runner.FirmwareRunner(state_dir=tmp_path)._env()
    assert "MQTT_PASSWORD" not in env and "MQTT_USERNAME" not in env
    assert env["FIRMWARE_STATE_DIR"] == str(tmp_path)


# =========================================================================== D: Denial of service

class FakeResponse:
    def __init__(self, size):
        self.size = size

    def read(self, n):
        return b"x" * min(n, self.size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_D1_oversized_download_is_refused(installer, monkeypatch):
    monkeypatch.setattr(installer.urllib.request, "urlopen",
                        lambda url, timeout: FakeResponse(installer.MAX_DOWNLOAD_BYTES + 1))
    with pytest.raises(installer.InstallError, match="is larger than"):
        installer.fetch("http://registry/packages/2.0", timeout=1)


def test_D1_forged_manifest_stops_before_the_package_is_downloaded(installer, signing_key,
                                                                  make_package, release, monkeypatch):
    """A forged command costs the device two small requests, never the package download."""
    manifest, _, package = make_package("2.0", [("main.py", "file", MAIN)])
    forged_sig = release.sign(manifest, release.Ed25519PrivateKey.generate())
    served = {"m": manifest, "s": forged_sig, "p": package}
    fetched = []
    monkeypatch.setattr(installer, "fetch", lambda url, timeout: fetched.append(url) or served[url])
    with pytest.raises(installer.VerificationError):
        installer.download_verified("m", "s", "p", signing_key.public_key(), "2.0",
                                    json.loads(manifest)["sha256"], 1)
    assert fetched == ["m", "s"]


@open_gap("D2", "no message size limit on the broker; one client can send very large messages")
def test_D2_broker_limits_message_size(read_repo):
    conf = mosquitto_settings(read_repo("deploy/mqtt/mosquitto.conf"))
    assert "max_packet_size" in conf or "message_size_limit" in conf


@pytest.mark.parametrize("field, value", [("error_count", 2**31), ("crash_count", 2**31),
                                          ("error_count", -1)])
def test_D3_out_of_range_counts_are_rejected_not_stored(messages, field, value):
    """Rejected at the door instead of failing inside Postgres."""
    payload = json.dumps({"schema_version": 1, "device_id": "device-001", "version": "1.0",
                          "status": "healthy", "error_count": 0, "timestamp": TS,
                          field: value}).encode()
    with pytest.raises(messages.InvalidMessage, match=field):
        messages.parse_heartbeat("device-001", payload)


# =========================================================================== E: Elevation of privilege

def runs_as_non_root(dockerfile: str) -> bool:
    users = [line.split()[1] for line in dockerfile.splitlines() if line.startswith("USER ")]
    return bool(users) and users[-1] not in ("root", "0")


def test_E1_device_agent_runs_as_non_root(read_repo):
    assert runs_as_non_root(read_repo("deploy/device-agent/Dockerfile"))


@open_gap("E1", "the orchestrator and software registry containers run as root")
@pytest.mark.parametrize("dockerfile", ["deploy/orchestrator/Dockerfile",
                                        "deploy/registries/software/Dockerfile"])
def test_E1_control_plane_services_run_as_non_root(read_repo, dockerfile):
    assert runs_as_non_root(read_repo(dockerfile))
