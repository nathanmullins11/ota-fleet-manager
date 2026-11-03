"""Software registry: serves versioned firmware packages to the fleet.

Packages are plain files in PACKAGES_DIR named <version><extension>, for example
1.0.txt, 2.0.txt, 2.1.tar.gz. The version is taken from the file name, and the
SHA-256 checksum is computed from the file contents, so adding a version means
dropping a file in the folder (no database or restart needed).

Endpoints:
  GET /health               liveness check
  GET /versions             list all available versions with checksums
  GET /versions/{version}   metadata for one version
  GET /packages/{version}   download the package file
"""
import hashlib
import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

PACKAGES_DIR = Path(os.environ.get("PACKAGES_DIR", Path(__file__).parent / "packages")).resolve()

# "2.1.tar.gz" -> version "2.1", extension ".tar.gz"; "1.0.txt" -> "1.0", ".txt"
FILENAME_RE = re.compile(r"^(?P<version>\d+\.\d+(?:\.\d+)?)(?P<ext>\..+)$")
VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")

app = FastAPI(title="OTA Software Registry")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan() -> dict[str, Path]:
    """Map version -> file for every valid package in PACKAGES_DIR."""
    found: dict[str, Path] = {}
    if not PACKAGES_DIR.is_dir():
        return found
    for path in sorted(PACKAGES_DIR.iterdir()):
        match = FILENAME_RE.match(path.name)
        if path.is_file() and match:
            found[match["version"]] = path
    return found


def _describe(version: str, path: Path) -> dict:
    return {
        "version": version,
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "download_url": f"/packages/{version}",
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "versions_available": len(_scan())}


@app.get("/versions")
def list_versions() -> list[dict]:
    packages = _scan()
    # Sort numerically so 2.10 comes after 2.9.
    ordered = sorted(packages, key=lambda v: tuple(int(p) for p in v.split(".")))
    return [_describe(v, packages[v]) for v in ordered]


@app.get("/versions/{version}")
def get_version(version: str) -> dict:
    path = _lookup(version)
    return _describe(version, path)


@app.get("/packages/{version}")
def download_package(version: str) -> FileResponse:
    path = _lookup(version)
    return FileResponse(
        path,
        filename=path.name,
        headers={"X-Checksum-SHA256": _sha256(path)},
    )


def _lookup(version: str) -> Path:
    # Validate before touching the filesystem (blocks path traversal like "../x").
    if not VERSION_RE.match(version):
        raise HTTPException(status_code=400, detail="Invalid version format")
    path = _scan().get(version)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Version {version} not found")
    return path