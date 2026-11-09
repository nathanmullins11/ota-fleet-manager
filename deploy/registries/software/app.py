"""Software registry: serves signed firmware packages to the fleet.

Each version is a folder in PACKAGES_DIR, built and signed by tools/release.py:

  2.0/firmware.tar.gz   the package
  2.0/manifest.json     version, size and SHA-256 of the package
  2.0/manifest.sig      Ed25519 signature over the exact bytes of manifest.json (base64)

The registry only serves versions that have all three files and a manifest that names
the folder's version (FR-6: only signed packages are published). It does not hold the
public key or check signatures: devices do that. So if this server, or a file on it,
is tampered with, devices still refuse the package.

Adding a version means copying its folder in; no restart or rebuild is needed.

Endpoints:
  GET /health                          liveness check (503 if a version folder is invalid)
  GET /versions                        all available versions with checksums
  GET /versions/{version}              metadata for one version
  GET /packages/{version}              download the package
  GET /packages/{version}/manifest     the manifest, byte for byte as signed
  GET /packages/{version}/manifest.sig the signature over the manifest
"""
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response

PACKAGES_DIR = Path(os.environ.get("PACKAGES_DIR", Path(__file__).parent / "packages")).resolve()

PACKAGE_FILE = "firmware.tar.gz"
MANIFEST_FILE = "manifest.json"
SIGNATURE_FILE = "manifest.sig"

# Always used with fullmatch() and re.ASCII: no trailing newline, digits 0-9 only.
VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?", re.ASCII)

log = logging.getLogger("software-registry")

app = FastAPI(title="OTA Software Registry")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _problem(version: str, folder: Path) -> str | None:
    """Why this version folder cannot be served, or None if it can."""
    for name in (PACKAGE_FILE, MANIFEST_FILE, SIGNATURE_FILE):
        if not (folder / name).is_file():
            return f"missing {name}"
    try:
        manifest = json.loads((folder / MANIFEST_FILE).read_bytes())
    except ValueError:
        return "manifest is not valid JSON"
    if not isinstance(manifest, dict) or manifest.get("version") != version:
        return "manifest version does not match folder name"
    return None


def _scan_all() -> tuple[dict[str, Path], dict[str, str]]:
    """Scan PACKAGES_DIR once.

    Returns (packages, invalid): packages maps version -> folder for every servable
    version; invalid maps version -> reason for every version folder that is not.
    Entries whose name is not a version are ignored.
    """
    packages: dict[str, Path] = {}
    invalid: dict[str, str] = {}
    if PACKAGES_DIR.is_dir():
        for folder in sorted(PACKAGES_DIR.iterdir()):
            if not (folder.is_dir() and VERSION_RE.fullmatch(folder.name)):
                continue
            problem = _problem(folder.name, folder)
            if problem:
                invalid[folder.name] = problem
            else:
                packages[folder.name] = folder
    return packages, invalid


def _scan() -> dict[str, Path]:
    packages, invalid = _scan_all()
    for version, reason in invalid.items():
        log.warning("Not serving version %s: %s", version, reason)
    return packages


def _describe(version: str, folder: Path) -> dict:
    package = folder / PACKAGE_FILE
    manifest = json.loads((folder / MANIFEST_FILE).read_bytes())
    return {
        "version": version,
        "filename": PACKAGE_FILE,
        "size_bytes": package.stat().st_size,
        # Computed from the file actually served, not copied from the manifest, so a
        # tampered file shows up as a mismatch on the device rather than being hidden.
        "sha256": _sha256(package),
        "notes": manifest.get("notes", ""),
        "download_url": f"/packages/{version}",
        "manifest_url": f"/packages/{version}/manifest",
        "signature_url": f"/packages/{version}/manifest.sig",
    }


@app.get("/health")
def health():
    packages, invalid = _scan_all()
    if invalid:
        # 503 makes the compose healthcheck fail, so the problem is visible in `docker compose ps`.
        return JSONResponse(status_code=503, content={
            "status": "error",
            "versions_available": len(packages),
            "invalid_versions": invalid,
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
    return _describe(version, _lookup(version))


@app.get("/packages/{version}")
def download_package(version: str) -> FileResponse:
    path = _lookup(version) / PACKAGE_FILE
    return FileResponse(
        path,
        media_type="application/gzip",
        filename=f"firmware-{version}.tar.gz",
        headers={"X-Checksum-SHA256": _sha256(path)},
    )


@app.get("/packages/{version}/manifest")
def get_manifest(version: str) -> Response:
    # Raw bytes: the signature covers them exactly, so they must not be re-serialized.
    return Response((_lookup(version) / MANIFEST_FILE).read_bytes(), media_type="application/json")


@app.get("/packages/{version}/manifest.sig")
def get_signature(version: str) -> Response:
    return Response((_lookup(version) / SIGNATURE_FILE).read_bytes(), media_type="text/plain")


def _lookup(version: str) -> Path:
    # Validate before touching the filesystem (blocks path traversal like "../x").
    if not VERSION_RE.fullmatch(version):
        raise HTTPException(status_code=400, detail="Invalid version format")
    folder = _scan().get(version)
    if folder is None:
        raise HTTPException(status_code=404, detail=f"Version {version} not found")
    return folder
