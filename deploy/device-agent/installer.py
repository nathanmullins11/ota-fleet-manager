"""Firmware packages on the device: download, verify, unpack, and remember what is installed.

Layout under the firmware folder:

  versions/<version>/manifest.json, manifest.sig, firmware.tar.gz   as downloaded (signed)
  versions/<version>/app/                                           unpacked, run from here
  versions/<version>/state-snapshot/                                device state as that version left it
  state/                                                            the firmware's persistent state
                                                                    (FIRMWARE_STATE_DIR)
  state.json                                                        {"current": ..., "previous": ...}

State policy (firmware/README.md, "State"): before every switch the agent snapshots state/
into the folder of the version it leaves; a rollback (or any move to an older state schema)
restores the target's snapshot, so older firmware never reads state written by newer firmware.

Only the current and previous versions are kept, so a rollback can reinstall the previous
one without downloading it. Every package is verified before it is unpacked, with the
checks of mqtt-topics-and-messages.md 4.3, and is verified and unpacked again just before
it is started (refresh_app), so an edited app/ folder never runs.
verify_package() matches tools/release.py's reference implementation (a test checks this).
"""
import base64
import hashlib
import http.client
import io
import json
import logging
import os
import shutil
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

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
DEVICE_STATE_DIR = "state"
SNAPSHOT_DIR = "state-snapshot"
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024     # firmware packages are a few KB


class VerificationError(Exception):
    """A package failed a check. The message is the reason reported in the event."""


class InstallError(Exception):
    """A package could not be downloaded or installed. The message is the event reason."""


class DownloadError(InstallError):
    """A download failed on the network, not a check: worth trying again if `retryable`."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


# --------------------------------------------------------------------------- verification

def load_public_key(path: Path) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(f"{path} is not an Ed25519 public key")
    return key


Report = Optional[Callable[[str], None]]


def _check(report: Report, number: int, name: str, test: Callable[[], None]) -> None:
    """Run one numbered check; tell `report` how it went (for the device log)."""
    try:
        test()
    except VerificationError as exc:
        if report:
            report(f"check {number}/4 {name}: FAILED ({exc})")
        raise
    if report:
        report(f"check {number}/4 {name}: OK")


def verify_manifest(manifest: bytes, signature: bytes, public_key: Ed25519PublicKey,
                    expected_version: str, expected_sha256: Optional[str] = None,
                    report: Report = None) -> dict:
    """Checks 1 to 3: signature, version, checksum against the command."""
    def signature_valid():
        try:
            public_key.verify(base64.b64decode(signature.strip(), validate=True), manifest)
        except (InvalidSignature, ValueError) as exc:
            raise VerificationError("signature verification failed") from exc

    fields: dict = {}

    def right_version():
        try:
            fields.update(json.loads(manifest))
        except (ValueError, TypeError) as exc:
            raise VerificationError("manifest is not valid JSON") from exc
        if not isinstance(fields.get("sha256"), str):
            raise VerificationError("manifest is missing required fields")
        if fields.get("version") != expected_version:
            raise VerificationError(
                f"manifest is for version {fields.get('version')!r}, expected {expected_version!r}")

    def matches_command():
        if expected_sha256 is not None and fields["sha256"] != expected_sha256:
            raise VerificationError("checksum in manifest does not match the command")

    _check(report, 1, "signature", signature_valid)
    _check(report, 2, f"manifest is for {expected_version}", right_version)
    _check(report, 3, "checksum matches the command" if expected_sha256 is not None
           else "checksum (no command to compare)", matches_command)
    return fields


def check_package(fields: dict, package: bytes, report: Report = None) -> None:
    """Check 4: the package's size and SHA-256 match the (already verified) manifest."""
    def matches_manifest():
        if fields.get("size_bytes") != len(package):
            raise VerificationError("package size does not match manifest")
        if hashlib.sha256(package).hexdigest() != fields["sha256"]:
            raise VerificationError("checksum mismatch")

    _check(report, 4, "package size and checksum", matches_manifest)


def verify_package(*, manifest: bytes, signature: bytes, package: bytes,
                   public_key: Ed25519PublicKey, expected_version: str,
                   expected_sha256: Optional[str] = None, report: Report = None) -> dict:
    """All four checks in order. Returns the manifest fields or raises VerificationError."""
    fields = verify_manifest(manifest, signature, public_key, expected_version, expected_sha256,
                             report)
    check_package(fields, package, report)
    return fields


# --------------------------------------------------------------------------- downloads

def fetch(url: str, timeout: float, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
    """GET a file. Network problems raise DownloadError, so they are never mistaken for a
    failed check; a connection that closes early is one of them (threat D8)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
            expected = resp.headers.get("Content-Length")
    except urllib.error.HTTPError as exc:
        # 4xx other than timeouts and rate limits will not change on a retry.
        retryable = exc.code >= 500 or exc.code in (408, 429)
        raise DownloadError(f"download failed: {url} returned HTTP {exc.code}",
                            retryable) from exc
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise DownloadError(f"download failed: {url}: {str(reason) or type(reason).__name__}"
                            ) from exc
    if len(data) > max_bytes:
        raise DownloadError(f"download failed: {url} is larger than {max_bytes} bytes",
                            retryable=False)
    if expected is not None and expected.isdigit() and len(data) < int(expected):
        raise DownloadError(f"download failed: {url}: connection closed after {len(data)} "
                            f"of {expected} bytes")
    return data


def fetch_with_retry(url: str, timeout: float, attempts: int = 1, retry_delay: float = 1.0,
                     report: Report = None) -> bytes:
    """fetch(), trying a network failure again up to `attempts` times in all, waiting
    retry_delay, then twice as long, and so on (threat D9: a brief outage is not a failed
    update)."""
    for attempt in range(1, attempts + 1):
        try:
            return fetch(url, timeout)
        except DownloadError as exc:
            if not exc.retryable or attempt == attempts:
                if attempts > 1 and exc.retryable:
                    raise DownloadError(f"{exc} (after {attempts} attempts)") from exc
                raise
            delay = retry_delay * 2 ** (attempt - 1)
            if report:
                report(f"{exc}; trying again in {delay:g}s (attempt {attempt + 1} of {attempts})")
            time.sleep(delay)
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class Download:
    manifest: bytes
    signature: bytes
    package: bytes


def download_verified(manifest_url: str, signature_url: str, package_url: str,
                      public_key: Ed25519PublicKey, version: str, sha256: str,
                      timeout: float, report: Report = None, attempts: int = 1,
                      retry_delay: float = 1.0) -> Download:
    """Fetch and verify a package. The manifest is checked before the package is fetched,
    so a forged or mismatched command never causes the package itself to be downloaded.
    Each file is tried up to `attempts` times if the network fails (fetch_with_retry)."""
    def get(url: str, what: str) -> bytes:
        data = fetch_with_retry(url, timeout, attempts, retry_delay, report)
        if report:
            report(f"downloaded {what} ({len(data)} bytes) from {url}")
        return data

    manifest = get(manifest_url, "manifest")
    signature = get(signature_url, "signature")
    fields = verify_manifest(manifest, signature, public_key, version, sha256, report)
    package = get(package_url, "package")
    check_package(fields, package, report)
    return Download(manifest, signature, package)


# --------------------------------------------------------------------------- local store

@dataclass(frozen=True)
class Installed:
    version: str
    app_dir: Path
    entrypoint: str
    state_schema: Optional[int] = None    # from the manifest; None: the firmware keeps no state


class Store:
    def __init__(self, root: Path, public_key: Ed25519PublicKey):
        self.root = Path(root)
        self.public_key = public_key
        self.versions_dir = self.root / "versions"
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.root / DEVICE_STATE_DIR
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

    def verify_local(self, version: str, expected_sha256: Optional[str] = None,
                     report: Report = None) -> dict:
        """Re-verify a stored package (all four checks). Raises VerificationError or OSError."""
        folder = self.versions_dir / version
        return verify_package(
            manifest=(folder / MANIFEST_FILE).read_bytes(),
            signature=(folder / SIGNATURE_FILE).read_bytes(),
            package=(folder / PACKAGE_FILE).read_bytes(),
            public_key=self.public_key, expected_version=version,
            expected_sha256=expected_sha256, report=report)

    def has_valid(self, version: str, expected_sha256: Optional[str] = None,
                  report: Report = None) -> bool:
        try:
            self.verify_local(version, expected_sha256, report)
            return (self.versions_dir / version / APP_DIR).is_dir()
        except (VerificationError, OSError) as exc:
            if (self.versions_dir / version).exists():
                log.warning("Stored copy of %s is not usable: %s", version, exc)
            return False

    def add(self, version: str, download: Download, report: Report = None) -> Installed:
        """Store and unpack a verified package. Replaces any earlier copy of that version."""
        fields = verify_package(manifest=download.manifest, signature=download.signature,
                                package=download.package, public_key=self.public_key,
                                expected_version=version, report=report)
        staging = self.versions_dir / f".{version}.new"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            _unpack(download.package, _entrypoint(fields), staging / APP_DIR)
        except InstallError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        (staging / MANIFEST_FILE).write_bytes(download.manifest)
        (staging / SIGNATURE_FILE).write_bytes(download.signature)
        (staging / PACKAGE_FILE).write_bytes(download.package)

        final = self.versions_dir / version
        if final.exists():
            if version == self.current:
                # The running version's folder is never replaced under it.
                shutil.rmtree(staging, ignore_errors=True)
                return self.installed(version)
            if (final / SNAPSHOT_DIR).is_dir():         # a fresh copy keeps the state snapshot
                os.replace(final / SNAPSHOT_DIR, staging / SNAPSHOT_DIR)
            shutil.rmtree(final)
        os.replace(staging, final)
        return self.installed(version)

    def refresh_app(self, version: str, report: Report = None) -> Installed:
        """Unpack app/ again from the stored package, after verifying it, and return it.

        Called just before a version is started (with no firmware running), so whatever is
        in the unpacked folder is discarded: firmware runs as the agent's user and could have
        edited it (threat T3). The package is read once and the same bytes are verified and
        unpacked. Raises VerificationError, InstallError or OSError.
        """
        folder = self.versions_dir / version
        download = Download((folder / MANIFEST_FILE).read_bytes(),
                            (folder / SIGNATURE_FILE).read_bytes(),
                            (folder / PACKAGE_FILE).read_bytes())
        fields = verify_package(manifest=download.manifest, signature=download.signature,
                                package=download.package, public_key=self.public_key,
                                expected_version=version, report=report)
        fresh = folder / f".{APP_DIR}.new"
        old = folder / f".{APP_DIR}.old"
        shutil.rmtree(fresh, ignore_errors=True)
        _unpack(download.package, _entrypoint(fields), fresh)
        shutil.rmtree(old, ignore_errors=True)
        if (folder / APP_DIR).exists():
            os.replace(folder / APP_DIR, old)
        os.replace(fresh, folder / APP_DIR)
        shutil.rmtree(old, ignore_errors=True)
        return self.installed(version)

    def installed(self, version: str) -> Installed:
        manifest = json.loads((self.versions_dir / version / MANIFEST_FILE).read_bytes())
        schema = manifest.get("state_schema")
        return Installed(version, self.versions_dir / version / APP_DIR,
                         manifest.get("entrypoint", "main.py"),
                         schema if type(schema) is int else None)

    # ---- device state

    def has_snapshot(self, version: str) -> bool:
        return (self.versions_dir / version / SNAPSHOT_DIR).is_dir()

    def snapshot_state(self, version: str) -> None:
        """Save the device state as `version` left it (call with the firmware stopped)."""
        dest = self.versions_dir / version / SNAPSHOT_DIR
        _copy_folder(self.state_dir, dest)

    def restore_state(self, version: str) -> None:
        """Replace the device state with the snapshot saved when `version` was left."""
        _copy_folder(self.versions_dir / version / SNAPSHOT_DIR, self.state_dir)

    def install_from_folder(self, folder: Path, report: Report = None) -> Installed:
        """Install a package from a folder holding the three published files (factory image)."""
        folder = Path(folder)
        download = Download((folder / MANIFEST_FILE).read_bytes(),
                            (folder / SIGNATURE_FILE).read_bytes(),
                            (folder / PACKAGE_FILE).read_bytes())
        return self.add(folder.name, download, report)

    def bootable(self, factory_dir: Path, report: Report = None) -> Installed:
        """The version to run at start-up: current, else previous, else the factory package.

        Each candidate is verified on disk first, so a damaged or edited copy is never run.
        """
        for version in (self.current, self.previous):
            if version and self.has_valid(version, report=report):
                if version != self.current:
                    log.warning("Current version %s is not usable; falling back to %s",
                                self.current, version)
                    if self.has_snapshot(version):
                        self.restore_state(version)
                        log.info("Restored the state snapshot of %s", version)
                    self.set_current(version)
                return self.installed(version)
        factory = sorted(p for p in Path(factory_dir).iterdir()
                         if p.is_dir() and VERSION_RE.fullmatch(p.name))
        if not factory:
            raise InstallError(f"no factory package in {factory_dir}")
        log.info("Installing factory firmware %s", factory[-1].name)
        installed = self.install_from_folder(factory[-1], report)
        self.set_current(installed.version)
        return installed


def _entrypoint(fields: dict) -> str:
    entrypoint = fields.get("entrypoint", "main.py")
    if not isinstance(entrypoint, str) or "/" in entrypoint or entrypoint in ("", ".", ".."):
        raise InstallError(f"manifest entrypoint {entrypoint!r} is not a file name")
    return entrypoint


def _unpack(package: bytes, entrypoint: str, dest: Path) -> None:
    """Unpack a verified package into the new folder `dest`; it must contain `entrypoint`."""
    dest.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as tar:
            tar.extractall(dest, filter="data")
    except (tarfile.TarError, OSError, EOFError) as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise InstallError(f"could not unpack the package: {exc}") from exc
    if not (dest / entrypoint).is_file():
        shutil.rmtree(dest, ignore_errors=True)
        raise InstallError(f"package has no {entrypoint}")


def _copy_folder(src: Path, dest: Path) -> None:
    """Make dest an exact copy of src (empty if src does not exist), swapping it in whole."""
    tmp = dest.with_name(dest.name + ".new")
    old = dest.with_name(dest.name + ".old")
    shutil.rmtree(tmp, ignore_errors=True)
    if src.is_dir():
        shutil.copytree(src, tmp)
    else:
        tmp.mkdir(parents=True)
    shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        os.replace(dest, old)
    os.replace(tmp, dest)
    shutil.rmtree(old, ignore_errors=True)
