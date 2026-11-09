#!/usr/bin/env python3
"""Release tool: create the release key pair, build and sign firmware packages.

  python tools/release.py keygen             # once: creates the release key pair
  python tools/release.py build 2.0          # package firmware/2.0 into the registry, signed
  python tools/release.py build --all        # every folder under firmware/
  python tools/release.py verify 2.0         # check a published package the way a device will
  python tools/release.py verify 2.0 --registry http://<vm-ip>:8080

A build writes three files to deploy/registries/software/packages/<version>/:

  firmware.tar.gz   the firmware folder, packed reproducibly (same source -> same bytes)
  manifest.json     version, file name, size and SHA-256 of firmware.tar.gz
  manifest.sig      Ed25519 signature over the exact bytes of manifest.json, base64

The private key stays on the release machine (keys/release-private.pem, git-ignored).
The public key is committed in keys/ and copied into the device agent, which uses it
to verify packages. verify_package() below is the reference for the agent's checks.
"""
import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

REPO = Path(__file__).resolve().parent.parent
FIRMWARE_DIR = REPO / "firmware"
PACKAGES_DIR = REPO / "deploy" / "registries" / "software" / "packages"
PRIVATE_KEY = Path(os.environ.get("RELEASE_PRIVATE_KEY", REPO / "keys" / "release-private.pem"))
PUBLIC_KEY = REPO / "keys" / "release-public.pem"
# Copies of the public key that ship with the devices.
PUBLIC_KEY_COPIES = [REPO / "deploy" / "device-agent" / "release-public.pem"]

PACKAGE_FILE = "firmware.tar.gz"
MANIFEST_FILE = "manifest.json"
SIGNATURE_FILE = "manifest.sig"
ENTRYPOINT = "main.py"
NOTES_FILE = "NOTES"                     # release note; goes in the manifest, not the package
EXCLUDED = {"__pycache__", NOTES_FILE}
MANIFEST_SCHEMA_VERSION = 1

# Same rule as the registry and the message contract.
VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?", re.ASCII)
SOURCE_VERSION_RE = re.compile(r'^VERSION = "([^"]*)"$', re.MULTILINE)


class ReleaseError(Exception):
    """A package cannot be built from this source."""


class VerificationError(Exception):
    """A package failed verification. The message is the reason a device reports."""


# --------------------------------------------------------------------------- keys

def generate_keys(private_path: Path, public_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    private_path.chmod(0o600)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_bytes(public_pem(key.public_key()))


def public_pem(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.PEM,
                            serialization.PublicFormat.SubjectPublicKeyInfo)


def load_private_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ReleaseError(f"{path} is not an Ed25519 private key")
    return key


def load_public_key(pem: bytes) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, Ed25519PublicKey):
        raise VerificationError("release public key is not an Ed25519 key")
    return key


# --------------------------------------------------------------------------- build

def source_version(src: Path) -> str:
    entry = src / ENTRYPOINT
    if not entry.is_file():
        raise ReleaseError(f"{src} has no {ENTRYPOINT}")
    match = SOURCE_VERSION_RE.search(entry.read_text())
    if not match:
        raise ReleaseError(f'{entry} does not define VERSION = "<version>"')
    return match[1]


def build_tarball(src: Path) -> bytes:
    """Pack src reproducibly: sorted entries, fixed timestamps, owners and modes."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(src.rglob("*")):
            rel = path.relative_to(src)
            if any(part in EXCLUDED for part in rel.parts) or path.suffix == ".pyc":
                continue
            info = tarfile.TarInfo(rel.as_posix())
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif path.is_file():
                data = path.read_bytes()
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
    # GzipFile, not gzip.compress(): compress() writes a different OS byte in the
    # header depending on the Python version, which would change the SHA-256.
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def build_manifest(version: str, package: bytes, notes: str) -> bytes:
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "version": version,
        "filename": PACKAGE_FILE,
        "entrypoint": ENTRYPOINT,
        "size_bytes": len(package),
        "sha256": hashlib.sha256(package).hexdigest(),
        "notes": notes,
    }
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()


def sign(manifest: bytes, key: Ed25519PrivateKey) -> bytes:
    return base64.b64encode(key.sign(manifest)) + b"\n"


def build(version: str, src: Path, out_root: Path, key: Ed25519PrivateKey) -> Path:
    """Build, sign and write one version. Returns the output folder."""
    if not VERSION_RE.fullmatch(version):
        raise ReleaseError(f"invalid version {version!r}")
    declared = source_version(src)
    if declared != version:
        raise ReleaseError(f"{src / ENTRYPOINT} declares VERSION {declared!r}, expected {version!r}")
    notes_path = src / NOTES_FILE
    notes = notes_path.read_text().strip() if notes_path.is_file() else ""

    package = build_tarball(src)
    manifest = build_manifest(version, package, notes)
    signature = sign(manifest, key)

    out = out_root / version
    out.mkdir(parents=True, exist_ok=True)
    (out / PACKAGE_FILE).write_bytes(package)
    (out / MANIFEST_FILE).write_bytes(manifest)
    (out / SIGNATURE_FILE).write_bytes(signature)
    return out


# --------------------------------------------------------------------------- verify

def verify_package(*, manifest: bytes, signature: bytes, package: bytes,
                   public_key: Ed25519PublicKey, expected_version: str,
                   expected_sha256: str | None = None) -> dict:
    """Run the device's checks in order. Returns the manifest, or raises VerificationError.

    1. The signature over the manifest bytes is valid for the release public key.
    2. The manifest is for the version that was asked for.
    3. The manifest's SHA-256 matches the one in the command (if given).
    4. The package's size and SHA-256 match the manifest.

    An edited package fails step 4, an edited manifest fails step 1, and an
    old-but-genuine manifest offered for another version fails step 2.
    """
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
    if fields.get("size_bytes") != len(package):
        raise VerificationError("package size does not match manifest")
    if hashlib.sha256(package).hexdigest() != fields["sha256"]:
        raise VerificationError("checksum mismatch")
    return fields


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.read()


def load_published(version: str, packages_dir: Path, registry: str | None) -> tuple[bytes, bytes, bytes]:
    """(manifest, signature, package) from a registry URL or the local packages folder."""
    if registry:
        base = f"{registry.rstrip('/')}/packages/{version}"
        return _fetch(f"{base}/manifest"), _fetch(f"{base}/manifest.sig"), _fetch(base)
    folder = packages_dir / version
    return ((folder / MANIFEST_FILE).read_bytes(), (folder / SIGNATURE_FILE).read_bytes(),
            (folder / PACKAGE_FILE).read_bytes())


# --------------------------------------------------------------------------- CLI

def _cmd_keygen(args) -> int:
    if PRIVATE_KEY.exists() and not args.force:
        print(f"{PRIVATE_KEY} already exists. Replacing it invalidates every signed package; "
              "use --force and rebuild all versions if you mean to.", file=sys.stderr)
        return 1
    generate_keys(PRIVATE_KEY, PUBLIC_KEY)
    for copy in PUBLIC_KEY_COPIES:
        shutil.copyfile(PUBLIC_KEY, copy)
    print(f"Private key: {PRIVATE_KEY} (keep it secret and backed up; never commit it)")
    print(f"Public key:  {PUBLIC_KEY}, copied to {', '.join(str(c) for c in PUBLIC_KEY_COPIES)}")
    return 0


def _cmd_build(args) -> int:
    if not PRIVATE_KEY.is_file():
        print(f"No private key at {PRIVATE_KEY}. Run `release.py keygen` or set "
              "RELEASE_PRIVATE_KEY.", file=sys.stderr)
        return 1
    key = load_private_key(PRIVATE_KEY)
    if public_pem(key.public_key()) != PUBLIC_KEY.read_bytes():
        print(f"{PRIVATE_KEY} does not match {PUBLIC_KEY}; devices would reject these packages.",
              file=sys.stderr)
        return 1
    versions = (sorted(p.name for p in FIRMWARE_DIR.iterdir()
                       if p.is_dir() and VERSION_RE.fullmatch(p.name))
                if args.all else args.versions)
    if not versions:
        print("Nothing to build: give versions or --all", file=sys.stderr)
        return 1
    for version in versions:
        try:
            out = build(version, FIRMWARE_DIR / version, PACKAGES_DIR, key)
        except ReleaseError as exc:
            print(f"{version}: {exc}", file=sys.stderr)
            return 1
        sha = json.loads((out / MANIFEST_FILE).read_bytes())["sha256"]
        print(f"{version}: {out.relative_to(REPO)}  sha256={sha}")
    return 0


def _cmd_verify(args) -> int:
    public_key = load_public_key(PUBLIC_KEY.read_bytes())
    status = 0
    for version in args.versions:
        try:
            manifest, signature, package = load_published(version, PACKAGES_DIR, args.registry)
            fields = verify_package(manifest=manifest, signature=signature, package=package,
                                    public_key=public_key, expected_version=version,
                                    expected_sha256=args.sha256)
            print(f"{version}: OK  sha256={fields['sha256']}")
        except (VerificationError, OSError) as exc:
            print(f"{version}: REJECTED  {exc}")
            status = 1
    return status


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("keygen", help="create the release key pair")
    p.add_argument("--force", action="store_true", help="replace an existing key pair")
    p.set_defaults(func=_cmd_keygen)

    p = sub.add_parser("build", help="build and sign firmware versions into the registry")
    p.add_argument("versions", nargs="*")
    p.add_argument("--all", action="store_true", help="build every folder under firmware/")
    p.set_defaults(func=_cmd_build)

    p = sub.add_parser("verify", help="verify published packages as a device would")
    p.add_argument("versions", nargs="+")
    p.add_argument("--registry", help="registry base URL; default is the local packages folder")
    p.add_argument("--sha256", help="expected SHA-256, as an update command would carry")
    p.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
