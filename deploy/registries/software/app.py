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
import logging
import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

PACKAGES_DIR = Path(os.environ.get("PACKAGES_DIR", Path(__file__).parent / "packages")).resolve()

# "2.1.tar.gz" -> version "2.1", extension ".tar.gz"; "1.0.txt" -> "1.0", ".txt"
# Always used with fullmatch() and re.ASCII: no trailing newline, digits 0-9 only.
FILENAME_RE = re.compile(r"(?P<version>\d+\.\d+(?:\.\d+)?)(?P<ext>\..+)", re.ASCII)
VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?", re.ASCII)

log = logging.getLogger("software-registry")

app = FastAPI(title="OTA Software Registry")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_all() -> tuple[dict[str, Path], dict[str, list[str]]]:
    """Scan PACKAGES_DIR once.

    Returns (packages, duplicates): packages maps version -> file for every version
    backed by exactly one file; duplicates maps version -> file names for every
    version claimed by more than one file (for example 1.0.txt and 1.0.tar.gz).
    """
    by_version: dict[str, list[Path]] = {}
    if PACKAGES_DIR.is_dir():
        for path in sorted(PACKAGES_DIR.iterdir()):
            match = FILENAME_RE.fullmatch(path.name)
            if path.is_file() and match:
                by_version.setdefault(match["version"], []).append(path)
    packages = {v: paths[0] for v, paths in by_version.items() if len(paths) == 1}
    duplicates = {v: [p.name for p in paths] for v, paths in by_version.items() if len(paths) > 1}
    return packages, duplicates


def _scan() -> dict[str, Path]:
    """Map version -> file for every unambiguous package in PACKAGES_DIR.

    A version claimed by more than one file is left out rather than served, because
    which file a device would receive would depend on sort order. /health reports it.
    """
    packages, duplicates = _scan_all()
    for version, names in duplicates.items():
        log.warning("Version %s is claimed by several files %s; not serving it", version, names)
    return packages


def _describe(version: str, path: Path) -> dict:
    return {
        "version": version,
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "download_url": f"/packages/{version}",
    }


@app.get("/health")
def health():
    packages, duplicates = _scan_all()
    if duplicates:
        # 503 makes the compose healthcheck fail, so the problem is visible in `docker compose ps`.
        return JSONResponse(status_code=503, content={
            "status": "error",
            "versions_available": len(packages),
            "duplicate_versions": duplicates,
        })
    return {"status": "ok", "versions_available": len(packages)}


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
    if not VERSION_RE.fullmatch(version):
        raise HTTPException(status_code=400, detail="Invalid version format")
    path = _scan().get(version)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Version {version} not found")
    return path