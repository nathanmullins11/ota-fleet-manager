"""Software registry (deploy/registries/software/app.py), run in-process with a temp packages folder."""
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


@pytest.fixture
def packages(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "PACKAGES_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client(registry):
    return TestClient(registry.app)


def publish(root, version, *, package=None, manifest=None, signature=b"c2ln\n",
            skip=()):
    """Write a version folder the way release.py lays it out (signature not checked here)."""
    folder = root / version
    folder.mkdir()
    package = package if package is not None else f"firmware {version}".encode()
    if manifest is None:
        manifest = json.dumps({"version": version, "notes": f"notes {version}",
                               "sha256": hashlib.sha256(package).hexdigest()}).encode()
    files = {"firmware.tar.gz": package, "manifest.json": manifest, "manifest.sig": signature}
    for name, data in files.items():
        if name not in skip:
            (folder / name).write_bytes(data)
    return folder


def test_scan_picks_valid_version_folders_only(registry, packages):
    for version in ["1.0", "2.1", "2.10"]:
        publish(packages, version)
    (packages / "notes.md").write_text("x")
    (packages / "1.0.txt").write_text("old flat layout")
    (packages / "1").mkdir()
    (packages / "١.٠").mkdir()
    assert sorted(registry._scan()) == ["1.0", "2.1", "2.10"]


def test_versions_sorted_numerically(client, packages):
    for version in ["2.10", "2.9", "1.0"]:
        publish(packages, version)
    assert [v["version"] for v in client.get("/versions").json()] == ["1.0", "2.9", "2.10"]


def test_version_metadata(client, packages):
    publish(packages, "2.0", package=b"pkg")
    assert client.get("/versions/2.0").json() == {
        "version": "2.0",
        "filename": "firmware.tar.gz",
        "size_bytes": 3,
        "sha256": hashlib.sha256(b"pkg").hexdigest(),
        "notes": "notes 2.0",
        "download_url": "/packages/2.0",
        "manifest_url": "/packages/2.0/manifest",
        "signature_url": "/packages/2.0/manifest.sig",
    }


def test_download_checksum_matches_body(client, packages):
    publish(packages, "2.0", package=b"firmware 2.0\n")
    resp = client.get("/packages/2.0")
    assert resp.status_code == 200
    assert resp.content == b"firmware 2.0\n"
    assert resp.headers["X-Checksum-SHA256"] == hashlib.sha256(resp.content).hexdigest()


def test_manifest_and_signature_served_byte_for_byte(client, packages):
    # Odd spacing on purpose: re-serializing the JSON would break the signature.
    manifest = b'{"version":  "2.0",\n "sha256": "ab"}\n'
    publish(packages, "2.0", manifest=manifest, signature=b"c2lnbmF0dXJl\n")
    assert client.get("/packages/2.0/manifest").content == manifest
    assert client.get("/packages/2.0/manifest.sig").content == b"c2lnbmF0dXJl\n"


def test_sha256_is_computed_from_the_served_file(client, packages):
    # A tampered package keeps its old manifest; the registry must not hide the change.
    folder = publish(packages, "2.0", package=b"original")
    (folder / "firmware.tar.gz").write_bytes(b"tampered")
    assert client.get("/versions/2.0").json()["sha256"] == hashlib.sha256(b"tampered").hexdigest()


# FR-6: only signed packages are published.
@pytest.mark.parametrize("missing", ["firmware.tar.gz", "manifest.json", "manifest.sig"])
def test_incomplete_version_is_not_served(client, packages, missing):
    publish(packages, "1.0", skip={missing})
    publish(packages, "2.0")

    assert [v["version"] for v in client.get("/versions").json()] == ["2.0"]
    for path in ["/versions/1.0", "/packages/1.0", "/packages/1.0/manifest",
                 "/packages/1.0/manifest.sig"]:
        assert client.get(path).status_code == 404

    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["invalid_versions"] == {"1.0": f"missing {missing}"}


@pytest.mark.parametrize("manifest, reason", [
    (b"not json", "manifest is not valid JSON"),
    (b'["2.0"]', "manifest version does not match folder name"),
    (b'{"version": "1.0"}', "manifest version does not match folder name"),
])
def test_bad_manifest_is_not_served(client, packages, manifest, reason):
    publish(packages, "2.0", manifest=manifest)
    assert client.get("/versions/2.0").status_code == 404
    assert client.get("/health").json()["invalid_versions"] == {"2.0": reason}


def test_health_ok_when_all_versions_valid(client, packages):
    publish(packages, "1.0")
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "versions_available": 1}


# A trailing newline, traversal or non-ASCII digits must not pass version validation.
@pytest.mark.parametrize("path", ["/versions/1.0%0A", "/packages/1.0%0A", "/versions/..%2F..%2Fetc",
                                  "/packages/%D9%A1.%D9%A0", "/packages/..%2F1.0/manifest",
                                  "/packages/1.0%0A/manifest.sig"])
def test_malformed_versions_rejected(client, packages, path):
    publish(packages, "1.0")
    assert client.get(path).status_code in (400, 404)


def test_committed_packages_are_all_servable(registry, monkeypatch):
    """The real packages folder has only complete, signed versions."""
    monkeypatch.setattr(registry, "PACKAGES_DIR", registry.Path(registry.__file__).parent / "packages")
    packages, invalid = registry._scan_all()
    assert invalid == {}
    assert sorted(packages) == ["1.0", "2.0", "2.1"]
