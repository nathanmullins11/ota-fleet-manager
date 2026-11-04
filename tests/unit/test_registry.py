"""Software registry (deploy/registries/software/app.py), run in-process with a temp packages folder."""
import hashlib

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


def test_scan_picks_valid_names_only(registry, packages):
    for name in ["1.0.txt", "2.1.tar.gz", "2.10.txt", "notes.md", "1.txt", "١.٠.txt"]:
        (packages / name).write_text(name)
    (packages / "3.0.d").mkdir()
    assert sorted(registry._scan()) == ["1.0", "2.1", "2.10"]


def test_versions_sorted_numerically(client, packages):
    for name in ["2.10.txt", "2.9.txt", "1.0.txt"]:
        (packages / name).write_text(name)
    assert [v["version"] for v in client.get("/versions").json()] == ["1.0", "2.9", "2.10"]


def test_download_checksum_matches_body(client, packages):
    (packages / "2.0.txt").write_bytes(b"firmware 2.0\n")
    resp = client.get("/packages/2.0")
    assert resp.status_code == 200
    assert resp.headers["X-Checksum-SHA256"] == hashlib.sha256(resp.content).hexdigest()


# Defect 7: two files claiming one version.
def test_duplicate_version_is_not_served(client, packages):
    (packages / "1.0.txt").write_text("a")
    (packages / "1.0.tar.gz").write_text("b")
    (packages / "2.0.txt").write_text("c")

    assert [v["version"] for v in client.get("/versions").json()] == ["2.0"]
    assert client.get("/versions/1.0").status_code == 404
    assert client.get("/packages/1.0").status_code == 404


def test_duplicate_version_makes_health_fail(client, packages):
    (packages / "1.0.txt").write_text("a")
    (packages / "1.0.tar.gz").write_text("b")
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["duplicate_versions"] == {"1.0": ["1.0.tar.gz", "1.0.txt"]}


def test_health_ok_without_duplicates(client, packages):
    (packages / "1.0.txt").write_text("a")
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "versions_available": 1}


# Defect 1 on the HTTP side: a trailing newline must not pass version validation.
@pytest.mark.parametrize("path", ["/versions/1.0%0A", "/packages/1.0%0A", "/versions/..%2F..%2Fetc",
                                  "/packages/%D9%A1.%D9%A0"])
def test_malformed_versions_rejected(client, packages, path):
    (packages / "1.0.txt").write_text("a")
    assert client.get(path).status_code in (400, 404)
