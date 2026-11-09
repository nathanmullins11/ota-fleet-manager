"""Release tool (tools/release.py): reproducible builds, signing and the device's verification checks."""
import hashlib
import io
import json
import tarfile

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def key(release):
    return release.Ed25519PrivateKey.generate()


@pytest.fixture
def src(tmp_path):
    folder = tmp_path / "src" / "2.0"
    folder.mkdir(parents=True)
    (folder / "main.py").write_text('VERSION = "2.0"\nprint("hi")\n')
    (folder / "NOTES").write_text("healthy update\n")
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"junk")
    return folder


@pytest.fixture
def built(release, src, key, tmp_path):
    out = release.build("2.0", src, tmp_path / "out", key)
    return {name: (out / name).read_bytes()
            for name in ("firmware.tar.gz", "manifest.json", "manifest.sig")}


def verify(release, key, files, **overrides):
    kwargs = dict(manifest=files["manifest.json"], signature=files["manifest.sig"],
                  package=files["firmware.tar.gz"], public_key=key.public_key(),
                  expected_version="2.0")
    kwargs.update(overrides)
    return release.verify_package(**kwargs)


# --------------------------------------------------------------------------- build

def test_build_is_reproducible(release, src, key, tmp_path, built):
    (src / "main.py").touch()     # a new mtime must not change the output
    again = release.build("2.0", src, tmp_path / "again", key)
    for name, data in built.items():
        assert (again / name).read_bytes() == data, name


def test_package_contains_source_only(built):
    with tarfile.open(fileobj=io.BytesIO(built["firmware.tar.gz"])) as tar:
        assert tar.getnames() == ["main.py"]
        member = tar.getmember("main.py")
        assert (member.mtime, member.uid, member.mode) == (0, 0, 0o644)


def test_manifest_describes_package(built):
    manifest = json.loads(built["manifest.json"])
    package = built["firmware.tar.gz"]
    assert manifest == {
        "schema_version": 1,
        "version": "2.0",
        "filename": "firmware.tar.gz",
        "entrypoint": "main.py",
        "size_bytes": len(package),
        "sha256": hashlib.sha256(package).hexdigest(),
        "notes": "healthy update",
    }


def test_build_refuses_version_mismatch(release, src, key, tmp_path):
    (src / "main.py").write_text('VERSION = "2.1"\n')
    with pytest.raises(release.ReleaseError, match="declares VERSION '2.1'"):
        release.build("2.0", src, tmp_path / "out", key)


@pytest.mark.parametrize("version", ["2", "2.0\n", "../2.0", "v2.0"])
def test_build_refuses_invalid_version(release, src, key, tmp_path, version):
    with pytest.raises(release.ReleaseError, match="invalid version"):
        release.build(version, src, tmp_path / "out", key)


def test_build_refuses_missing_entrypoint(release, src, key, tmp_path):
    (src / "main.py").unlink()
    with pytest.raises(release.ReleaseError, match="has no main.py"):
        release.build("2.0", src, tmp_path / "out", key)


def test_generated_keys_round_trip(release, tmp_path):
    private, public = tmp_path / "k" / "priv.pem", tmp_path / "k" / "pub.pem"
    release.generate_keys(private, public)
    assert private.stat().st_mode & 0o777 == 0o600
    key = release.load_private_key(private)
    assert release.public_pem(key.public_key()) == public.read_bytes()


# --------------------------------------------------------------------------- verify

def test_genuine_package_verifies(release, key, built):
    sha = hashlib.sha256(built["firmware.tar.gz"]).hexdigest()
    assert verify(release, key, built, expected_sha256=sha)["version"] == "2.0"


def test_tampered_package_rejected(release, key, built):
    package = bytearray(built["firmware.tar.gz"])
    package[-1] ^= 0xFF
    with pytest.raises(release.VerificationError, match="checksum mismatch"):
        verify(release, key, built, package=bytes(package))


def test_tampered_manifest_rejected(release, key, built):
    # An attacker who swaps the package and rewrites the manifest's hash to match.
    evil = b"evil firmware"
    manifest = json.loads(built["manifest.json"])
    manifest.update(sha256=hashlib.sha256(evil).hexdigest(), size_bytes=len(evil))
    with pytest.raises(release.VerificationError, match="signature verification failed"):
        verify(release, key, built, package=evil,
               manifest=(json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())


def test_whitespace_change_to_manifest_rejected(release, key, built):
    with pytest.raises(release.VerificationError, match="signature verification failed"):
        verify(release, key, built, manifest=built["manifest.json"] + b" ")


def test_signature_from_another_key_rejected(release, key, built):
    other = release.Ed25519PrivateKey.generate()
    with pytest.raises(release.VerificationError, match="signature verification failed"):
        verify(release, key, built, signature=release.sign(built["manifest.json"], other))


@pytest.mark.parametrize("signature", [b"", b"not base64!\n", b"AAAA\n"])
def test_garbage_signature_rejected(release, key, built, signature):
    with pytest.raises(release.VerificationError, match="signature verification failed"):
        verify(release, key, built, signature=signature)


def test_genuine_package_for_another_version_rejected(release, key, built):
    # Replay: a correctly signed 2.0 offered when the command asked for 2.1.
    with pytest.raises(release.VerificationError, match="manifest is for version '2.0'"):
        verify(release, key, built, expected_version="2.1")


def test_command_checksum_mismatch_rejected(release, key, built):
    with pytest.raises(release.VerificationError, match="does not match the command"):
        verify(release, key, built, expected_sha256="0" * 64)


# --------------------------------------------------------------------------- committed state

def test_committed_packages_verify_with_committed_key(release):
    public_key = release.load_public_key(release.PUBLIC_KEY.read_bytes())
    for version in ["1.0", "2.0", "2.1"]:
        manifest, signature, package = release.load_published(version, release.PACKAGES_DIR, None)
        release.verify_package(manifest=manifest, signature=signature, package=package,
                               public_key=public_key, expected_version=version)


def test_committed_packages_match_source(release):
    """Fails if firmware/<v> was edited without rebuilding (`release.py build --all`)."""
    for version in ["1.0", "2.0", "2.1"]:
        published = (release.PACKAGES_DIR / version / release.PACKAGE_FILE).read_bytes()
        assert release.build_tarball(release.FIRMWARE_DIR / version) == published, version


def test_device_agent_has_the_release_public_key(release):
    for copy in release.PUBLIC_KEY_COPIES:
        assert copy.read_bytes() == release.PUBLIC_KEY.read_bytes(), copy
