"""Firmware packages on the device: download, verify, unpack, and remember what is installed.

Layout under the firmware folder:

  versions/<version>/manifest.json, manifest.sig, firmware.tar.gz   as downloaded (signed)
  versions/<version>/app/                                           unpacked, run from here
  state.json                                                        {"current": ..., "previous": ...}

Only the current and previous versions are kept, so a rollback can reinstall the previous
one without downloading it. Every package is verified before it is unpacked and again
before it is run from disk, with the checks of mqtt-topics-and-messages.md 4.3.
verify_package() matches tools/release.py's reference implementation (a test checks this).
"""
import base64
import hashlib
import io
import json
import logging
import os
import shutil
import tarfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from config import VERSION_RE

log = logging.getLogger("device-agent.installer")

MANIFEST_FILE = "manifest.json"
SIGNATURE_FILE = "manifest.sig"
PACKAGE_FILE = "firmware.tar.gz"
APP_DIR = "app"
STATE_FILE = "state.json"
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024     # firmware packages are a few KB


class VerificationError(Exception):
    """A package failed a check. The message is the reason reported in the event."""


class InstallError(Exception):
    """A package could not be downloaded or installed. The message is the event reason."""


# --------------------------------------------------------------------------- verification

def load_public_key(path: Path) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(f"{path} is not an Ed25519 public key")
    return key


def verify_manifest(manifest: bytes, signature: bytes, public_key: Ed25519PublicKey,
                    expected_version: str, expected_sha256: Optional[str] = None) -> dict:
    """Checks 1 to 3: signature, version, checksum against the command."""
    try:
        public_key.verify(base64.b64decode(signature.strip(), validate=True), manifest)
    except (InvalidSignature, ValueError) as exc:
        raise VerificationError("signature verification failed") from exc
    try:
        fields = json.loads(manifest)
    except ValueError as exc:
        raise VerificationError("manifest is not valid JSON") from exc
    if not isinstance(fields, dict) or not isinstance(fields.get("sha256"), str):
        raise VerificationError("manifest is missing required fields")
    if fields.get("version") != expected_version:
        raise VerificationError(
            f"manifest is for version {fields.get('version')!r}, expected {expected_version!r}")
    if expected_sha256 is not None and fields["sha256"] != expected_sha256:
        raise VerificationError("checksum in manifest does not match the command")
    return fields


def check_package(fields: dict, package: bytes) -> None:
    """Check 4: the package's size and SHA-256 match the (already verified) manifest."""
    if fields.get("size_bytes") != len(package):
        raise VerificationError("package size does not match manifest")
    if hashlib.sha256(package).hexdigest() != fields["sha256"]:
        raise VerificationError("checksum mismatch")


def verify_package(*, manifest: bytes, signature: bytes, package: bytes,
                   public_key: Ed25519PublicKey, expected_version: str,
                   expected_sha256: Optional[str] = None) -> dict:
    """All four checks in order. Returns the manifest fields or raises VerificationError."""
    fields = verify_manifest(manifest, signature, public_key, expected_version, expected_sha256)
    check_package(fields, package)
    return fields


# --------------------------------------------------------------------------- downloads

def fetch(url: str, timeout: float, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise InstallError(f"download failed: {url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise InstallError(f"download failed: {url}: {reason}") from exc
    if len(data) > max_bytes:
        raise InstallError(f"download failed: {url} is larger than {max_bytes} bytes")
    return data


@dataclass(frozen=True)
class Download:
    manifest: bytes
    signature: bytes
    package: bytes


def download_verified(manifest_url: str, signature_url: str, package_url: str,
                      public_key: Ed25519PublicKey, version: str, sha256: str,
                      timeout: float) -> Download:
    """Fetch and verify a package. The manifest is checked before the package is fetched."""
    manifest = fetch(manifest_url, timeout)
    signature = fetch(signature_url, timeout)
    fields = verify_manifest(manifest, signature, public_key, version, sha256)
    package = fetch(package_url, timeout)
    check_package(fields, package)
    return Download(manifest, signature, package)


# --------------------------------------------------------------------------- local store

@dataclass(frozen=True)
class Installed:
    version: str
    app_dir: Path
    entrypoint: str


class Store:
    def __init__(self, root: Path, public_key: Ed25519PublicKey):
        self.root = Path(root)
        self.public_key = public_key
        self.versions_dir = self.root / "versions"
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self._state = self._load_state()

    # ---- state

    @property
    def current(self) -> Optional[str]:
        return self._state.get("current")

    @property
    def previous(self) -> Optional[str]:
        return self._state.get("previous")

    def _load_state(self) -> dict:
        try:
            state = json.loads((self.root / STATE_FILE).read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(state, dict):
            return {}
        return {k: v for k, v in state.items()
                if k in ("current", "previous") and isinstance(v, str) and VERSION_RE.fullmatch(v)}

    def _save_state(self) -> None:
        tmp = self.root / (STATE_FILE + ".tmp")
        tmp.write_text(json.dumps(self._state) + "\n")
        os.replace(tmp, self.root / STATE_FILE)

    def set_current(self, version: str) -> None:
        """Record a switch to `version`; the old current becomes the previous version."""
        if version != self.current:
            self._state = {"current": version, "previous": self.current}
            if self._state["previous"] is None:
                del self._state["previous"]
        self._save_state()
        self._prune()

    def _prune(self) -> None:
        keep = {self.current, self.previous}
        for folder in self.versions_dir.iterdir():
            if folder.name not in keep:
                shutil.rmtree(folder, ignore_errors=True)

    # ---- packages

    def verify_local(self, version: str, expected_sha256: Optional[str] = None) -> dict:
        """Re-verify a stored package (all four checks). Raises VerificationError or OSError."""
        folder = self.versions_dir / version
        return verify_package(
            manifest=(folder / MANIFEST_FILE).read_bytes(),
            signature=(folder / SIGNATURE_FILE).read_bytes(),
            package=(folder / PACKAGE_FILE).read_bytes(),
            public_key=self.public_key, expected_version=version,
            expected_sha256=expected_sha256)

    def has_valid(self, version: str, expected_sha256: Optional[str] = None) -> bool:
        try:
            self.verify_local(version, expected_sha256)
            return (self.versions_dir / version / APP_DIR).is_dir()
        except (VerificationError, OSError) as exc:
            if (self.versions_dir / version).exists():
                log.warning("Stored copy of %s is not usable: %s", version, exc)
            return False

    def add(self, version: str, download: Download) -> Installed:
        """Store and unpack a verified package. Replaces any earlier copy of that version."""
        fields = verify_package(manifest=download.manifest, signature=download.signature,
                                package=download.package, public_key=self.public_key,
                                expected_version=version)
        entrypoint = fields.get("entrypoint", "main.py")
        if not isinstance(entrypoint, str) or "/" in entrypoint or entrypoint in ("", ".", ".."):
            raise InstallError(f"manifest entrypoint {entrypoint!r} is not a file name")

        staging = self.versions_dir / f".{version}.new"
        shutil.rmtree(staging, ignore_errors=True)
        (staging / APP_DIR).mkdir(parents=True)
        try:
            with tarfile.open(fileobj=io.BytesIO(download.package), mode="r:gz") as tar:
                tar.extractall(staging / APP_DIR, filter="data")
        except (tarfile.TarError, OSError, EOFError) as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise InstallError(f"could not unpack the package: {exc}") from exc
        if not (staging / APP_DIR / entrypoint).is_file():
            shutil.rmtree(staging, ignore_errors=True)
            raise InstallError(f"package has no {entrypoint}")
        (staging / MANIFEST_FILE).write_bytes(download.manifest)
        (staging / SIGNATURE_FILE).write_bytes(download.signature)
        (staging / PACKAGE_FILE).write_bytes(download.package)

        final = self.versions_dir / version
        if final.exists():
            if version == self.current:
                # The running version's folder is never replaced under it.
                shutil.rmtree(staging, ignore_errors=True)
                return self.installed(version)
            shutil.rmtree(final)
        os.replace(staging, final)
        return self.installed(version)

    def installed(self, version: str) -> Installed:
        manifest = json.loads((self.versions_dir / version / MANIFEST_FILE).read_bytes())
        return Installed(version, self.versions_dir / version / APP_DIR,
                         manifest.get("entrypoint", "main.py"))

    def install_from_folder(self, folder: Path) -> Installed:
        """Install a package from a folder holding the three published files (factory image)."""
        folder = Path(folder)
        download = Download((folder / MANIFEST_FILE).read_bytes(),
                            (folder / SIGNATURE_FILE).read_bytes(),
                            (folder / PACKAGE_FILE).read_bytes())
        return self.add(folder.name, download)

    def bootable(self, factory_dir: Path) -> Installed:
        """The version to run at start-up: current, else previous, else the factory package.

        Each candidate is verified on disk first, so a damaged or edited copy is never run.
        """
        for version in (self.current, self.previous):
            if version and self.has_valid(version):
                if version != self.current:
                    log.warning("Current version %s is not usable; falling back to %s",
                                self.current, version)
                    self.set_current(version)
                return self.installed(version)
        factory = sorted(p for p in Path(factory_dir).iterdir()
                         if p.is_dir() and VERSION_RE.fullmatch(p.name))
        if not factory:
            raise InstallError(f"no factory package in {factory_dir}")
        log.info("Installing factory firmware %s", factory[-1].name)
        installed = self.install_from_folder(factory[-1])
        self.set_current(installed.version)
        return installed
