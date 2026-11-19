"""Helpers and fixtures shared by the STRIDE security tests (docs/threat-model.md).

Test IDs (S1, T3, ...) match the threats in the threat model. A test for a threat that is
not mitigated yet is marked open_gap(...): xfail(strict=True), so it passes CI while the gap
is open and fails as soon as the gap is closed; the marker is then removed and the test
becomes a guard against regressions.

tests/security/conftest.py makes the fixtures below available to the tests in this folder.
"""
import http.server
import io
import json
import tarfile
import threading
from pathlib import Path

import pytest


def open_gap(threat: str, why: str):
    """Mark a test whose secure behaviour is not implemented yet (see module docstring)."""
    return pytest.mark.xfail(strict=True, reason=f"{threat} open: {why}")


REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def read_repo():
    """read_repo('deploy/mqtt/mosquitto.conf') -> text of a file in the repository."""
    return lambda rel: (REPO / rel).read_text()


# --------------------------------------------------------------------------- signed packages

@pytest.fixture
def make_package(release, signing_key):
    """make_package(version, members, key=signing_key, **manifest_extra) -> (manifest, sig, tar).

    members: list of (name, kind, data) where kind is 'file', 'symlink', 'hardlink' or 'dir';
    for links, data is the link target. The manifest is signed, so only the device's checks
    on the package's contents stand between it and the device.
    """
    def _make(version, members, key=None, **extra):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, kind, data in members:
                info = tarfile.TarInfo(name)
                if kind == "file":
                    payload = data.encode() if isinstance(data, str) else data
                    info.size = len(payload)
                    tar.addfile(info, io.BytesIO(payload))
                    continue
                info.type = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE,
                             "dir": tarfile.DIRTYPE}[kind]
                if kind in ("symlink", "hardlink"):
                    info.linkname = data
                tar.addfile(info)
        package = buf.getvalue()
        manifest = release.build_manifest(version, package, "", None)
        if extra:
            fields = json.loads(manifest)
            fields.update(extra)
            manifest = (json.dumps(fields, indent=2, sort_keys=True) + "\n").encode()
        return manifest, release.sign(manifest, key or signing_key), package
    return _make


# --------------------------------------------------------------------------- HTTP servers

class FileServer:
    """Serves bytes from a dict of path -> bytes; records every request path."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.requests: list[str] = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                server.requests.append(self.path)
                data = server.files.get(self.path)
                if data is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def publish(self, version: str, folder: Path = None, *, manifest=None, signature=None,
                package=None):
        """Serve a version with the registry's URL layout, from a folder and/or raw bytes."""
        def pick(given, name):
            return given if given is not None else (folder / name).read_bytes()
        base = f"/packages/{version}"
        self.files[base] = pick(package, "firmware.tar.gz")
        self.files[f"{base}/manifest"] = pick(manifest, "manifest.json")
        self.files[f"{base}/manifest.sig"] = pick(signature, "manifest.sig")

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def registry(packages):
    """The genuine registry: every test package, signed with the release (test) key."""
    server = FileServer()
    for folder in packages.iterdir():
        server.publish(folder.name, folder)
    yield server
    server.stop()


@pytest.fixture
def attacker():
    """A second server the attacker controls; starts empty."""
    server = FileServer()
    yield server
    server.stop()


# --------------------------------------------------------------------------- a real device

@pytest.fixture
def device(agent, agent_config, installer, firmware_runner, signing_key, packages, tmp_path,
           monkeypatch):
    """agent.Device on factory firmware 1.0, real firmware processes, events collected."""
    monkeypatch.setenv("FIRMWARE_TICK_SECONDS", "0.05")
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
    store = installer.Store(cfg.firmware_dir, signing_key.public_key())
    dev = agent.Device(cfg, store,
                       firmware_runner.FirmwareRunner(cfg.restart_delay_seconds, store.state_dir),
                       lambda *e: events.append(e))
    dev.events = events
    dev.boot()
    yield dev
    dev.runner.stop()


@pytest.fixture
def command(agent_commands):
    """command(server, kind, version, sha256=None, rollout_id=7) -> agent Command."""
    def _command(server, kind, version, sha256=None, rollout_id=7):
        base = f"{server.url}/packages/{version}"
        if sha256 is None:
            sha256 = json.loads(server.files[f"/packages/{version}/manifest"])["sha256"]
        return agent_commands.Command(kind, rollout_id, version, base, f"{base}/manifest",
                                      f"{base}/manifest.sig", sha256)
    return _command
