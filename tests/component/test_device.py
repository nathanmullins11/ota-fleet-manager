"""Device agent: update and rollback handling end to end, without MQTT (agent.Device).

A small HTTP server serves signed test packages with the registry's URL layout, and the
real firmware runs as child processes. Events the device would publish are collected.
"""
import http.server
import json
import threading
import time

import pytest

pytestmark = pytest.mark.component


class Registry:
    """Serves <packages>/<v>/ as /packages/<v>, /packages/<v>/manifest, /packages/<v>/manifest.sig."""

    def __init__(self, packages):
        self.packages = packages
        self.tamper = set()          # versions whose package bytes are altered when served
        self.requests = []
        registry = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                registry.requests.append(self.path)
                parts = self.path.strip("/").split("/")
                files = {2: "firmware.tar.gz", 3: {"manifest": "manifest.json",
                                                   "manifest.sig": "manifest.sig"}}
                try:
                    if len(parts) == 2:
                        name = files[2]
                    else:
                        name = files[3][parts[2]]
                    data = (registry.packages / parts[1] / name).read_bytes()
                except (KeyError, IndexError, OSError):
                    self.send_error(404)
                    return
                if len(parts) == 2 and parts[1] in registry.tamper:
                    data = data[:-1] + bytes([data[-1] ^ 0xFF])
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def registry(packages):
    r = Registry(packages)
    yield r
    r.stop()


@pytest.fixture(autouse=True)
def fast_firmware(monkeypatch):
    monkeypatch.setenv("FIRMWARE_TICK_SECONDS", "0.05")
    monkeypatch.setenv("FIRMWARE_FAULT_START_SECONDS", "0.6")
    monkeypatch.setenv("FIRMWARE_FAULT_CRASH_AFTER_SECONDS", "0.3")


@pytest.fixture
def device(agent, agent_config, installer, firmware_runner, signing_key, packages, tmp_path):
    factory = tmp_path / "factory" / "1.0"
    factory.mkdir(parents=True)
    for f in (packages / "1.0").iterdir():
        (factory / f.name).write_bytes(f.read_bytes())
    cfg = agent_config.Config(
        device_id="device-001", mqtt_host="unused", mqtt_port=1883,
        heartbeat_interval_seconds=5, log_level="INFO", firmware_dir=tmp_path / "fw",
        factory_dir=factory.parent, public_key_path=tmp_path / "unused.pem",
        start_timeout_seconds=3, restart_delay_seconds=0.2, download_timeout_seconds=3)
    events = []
    dev = agent.Device(cfg, installer.Store(cfg.firmware_dir, signing_key.public_key()),
                       firmware_runner.FirmwareRunner(cfg.restart_delay_seconds),
                       lambda *e: events.append(e))
    dev.events = events
    dev.boot()
    yield dev
    dev.runner.stop()


def sha(packages, version):
    return json.loads((packages / version / "manifest.json").read_bytes())["sha256"]


def command(agent_commands, registry, kind, version, rollout_id=7, sha256=None):
    base = f"{registry.url}/packages/{version}"
    return agent_commands.Command(kind, rollout_id, version, base, f"{base}/manifest",
                                  f"{base}/manifest.sig", sha256 or sha(registry.packages, version))


def names(device):
    return [(e[0], e[3]) for e in device.events]


def test_boots_on_factory_firmware(device):
    hb = device.heartbeat()
    assert (hb["version"], hb["status"], hb["crash_count"]) == ("1.0", "healthy", 0)
    assert device.runner.version == "1.0"


def test_update_installs_and_runs_new_version(device, agent_commands, registry):
    device.handle(command(agent_commands, registry, "update", "2.0"))
    assert names(device) == [("update_started", None), ("update_succeeded", None)]
    assert all(e[1:3] == (7, "2.0") for e in device.events)
    assert device.heartbeat()["version"] == "2.0" and device.runner.version == "2.0"
    assert (device.store.current, device.store.previous) == ("2.0", "1.0")


def test_repeated_command_is_harmless(device, agent_commands, registry):
    device.handle(command(agent_commands, registry, "update", "2.0"))
    registry.requests.clear()
    device.handle(command(agent_commands, registry, "update", "2.0"))
    assert names(device)[-1] == ("update_succeeded", "already running 2.0")
    assert registry.requests == []


def test_forged_command_checksum_is_refused(device, agent_commands, registry):
    device.handle(command(agent_commands, registry, "update", "2.0", sha256="0" * 64))
    assert names(device) == [("update_started", None),
                             ("update_failed", "checksum in manifest does not match the command")]
    assert device.heartbeat()["version"] == "1.0"
    assert not any(r == "/packages/2.0" for r in registry.requests)   # package never fetched


def test_tampered_package_is_refused(device, agent_commands, registry):
    registry.tamper.add("2.0")
    device.handle(command(agent_commands, registry, "update", "2.0"))
    assert names(device)[-1] == ("update_failed", "checksum mismatch")
    assert device.runner.version == "1.0"


def test_unreachable_registry_fails_the_update(device, agent_commands, registry):
    cmd = command(agent_commands, registry, "update", "2.0")
    registry.stop()
    device.handle(cmd)
    assert names(device)[-1][0] == "update_failed"
    assert names(device)[-1][1].startswith("download failed")


def test_firmware_that_does_not_start_is_reverted(device, agent_commands, registry):
    device.handle(command(agent_commands, registry, "update", "9.0"))
    event, reason = names(device)[-1]
    assert event == "update_failed"
    assert "exited with code 3" in reason and reason.endswith("still running 1.0")
    assert device.runner.version == "1.0" and device.store.current == "1.0"
    time.sleep(0.3)
    assert device.heartbeat()["status"] == "healthy"


def test_rollback_uses_the_kept_copy(device, agent_commands, registry):
    device.handle(command(agent_commands, registry, "update", "2.0"))
    rollback = command(agent_commands, registry, "rollback", "1.0")
    registry.stop()                          # proves no download is needed
    device.handle(rollback)
    assert names(device)[-2:] == [("rollback_started", None), ("rollback_succeeded", None)]
    assert device.heartbeat()["version"] == "1.0" and device.runner.version == "1.0"


def test_rollback_when_already_on_target(device, agent_commands, registry):
    device.handle(command(agent_commands, registry, "rollback", "1.0"))
    assert names(device) == [("rollback_succeeded", "already running 1.0")]


def test_faulty_update_crashes_are_reported_then_rolled_back(device, agent_commands, registry):
    device.handle(command(agent_commands, registry, "update", "2.1"))
    assert names(device)[-1] == ("update_succeeded", None)       # 2.1 starts healthy
    device.heartbeat()
    time.sleep(1.5)
    hb = device.heartbeat()
    assert hb["version"] == "2.1" and hb["crash_count"] >= 1 and hb["error_count"] > 0
    assert hb["status"] == "unhealthy"

    device.handle(command(agent_commands, registry, "rollback", "1.0"))
    assert names(device)[-1] == ("rollback_succeeded", None)
    device.heartbeat()
    time.sleep(1.5)
    hb = device.heartbeat()
    assert (hb["version"], hb["status"], hb["crash_count"]) == ("1.0", "healthy", 0)


def test_heartbeat_says_updating_while_busy(device):
    device.busy.set()
    assert device.heartbeat()["status"] == "updating"
