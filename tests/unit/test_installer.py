"""Device agent: package verification and the on-device store (installer.py)."""
import hashlib
import io
import json
import tarfile

import pytest

pytestmark = pytest.mark.unit


def files(packages, version) -> dict:
    return {name: (packages / version / name).read_bytes()
            for name in ("manifest.json", "manifest.sig", "firmware.tar.gz")}


def tamper_cases(release, signing_key, f):
    """(description, verify_package kwargs) for every attack the reference tests cover."""
    package = bytearray(f["firmware.tar.gz"]); package[-1] ^= 0xFF
    evil = b"evil firmware"
    forged = json.loads(f["manifest.json"])
    forged.update(sha256=hashlib.sha256(evil).hexdigest(), size_bytes=len(evil))
    other = release.Ed25519PrivateKey.generate()
    sha = json.loads(f["manifest.json"])["sha256"]
    base = dict(manifest=f["manifest.json"], signature=f["manifest.sig"],
                package=f["firmware.tar.gz"], public_key=signing_key.public_key(),
                expected_version="2.0")
    cases = {
        "genuine": {},
        "genuine with command checksum": {"expected_sha256": sha},
        "edited package": {"package": bytes(package)},
        "truncated package": {"package": f["firmware.tar.gz"][:-10]},
        "forged manifest": {"manifest": json.dumps(forged).encode(), "package": evil},
        "whitespace in manifest": {"manifest": f["manifest.json"] + b" "},
        "other key": {"signature": release.sign(f["manifest.json"], other)},
        "empty signature": {"signature": b""},
        "garbage signature": {"signature": b"not base64!\n"},
        "replayed version": {"expected_version": "2.1"},
        "command checksum differs": {"expected_sha256": "0" * 64},
    }
    return {name: {**base, **change} for name, change in cases.items()}


def outcome(module, kwargs):
    try:
        return module.verify_package(**kwargs)["sha256"]
    except module.VerificationError as exc:
        return f"rejected: {exc}"


def test_agent_verification_matches_reference(installer, release, signing_key, packages):
    """The agent's checks must give the same verdict and reason as tools/release.py."""
    for name, kwargs in tamper_cases(release, signing_key, files(packages, "2.0")).items():
        assert outcome(installer, kwargs) == outcome(release, kwargs), name
    verdicts = {n: outcome(installer, k)
                for n, k in tamper_cases(release, signing_key, files(packages, "2.0")).items()}
    assert sum(not v.startswith("rejected") for v in verdicts.values()) == 2


@pytest.fixture
def store(installer, signing_key, tmp_path):
    return installer.Store(tmp_path / "fw", signing_key.public_key())


def download(installer, packages, version):
    f = files(packages, version)
    return installer.Download(f["manifest.json"], f["manifest.sig"], f["firmware.tar.gz"])


def _factory(packages):
    """A factory folder holding only 1.0, like deploy/device-agent/factory/."""
    return packages.parent / f"{packages.name}-factory"


@pytest.fixture(autouse=True)
def factory_dir(packages):
    folder = _factory(packages)
    if not folder.exists():
        (folder / "1.0").mkdir(parents=True)
        for name, data in files(packages, "1.0").items():
            (folder / "1.0" / name).write_bytes(data)
    return folder


def test_first_boot_installs_factory_package(store, factory_dir):
    installed = store.bootable(factory_dir)
    assert installed.version == "1.0"
    assert (installed.app_dir / installed.entrypoint).is_file()
    assert store.current == "1.0" and store.previous is None


def test_switching_keeps_current_and_previous_only(installer, store, packages, factory_dir):
    store.bootable(factory_dir)
    for version in ("2.0", "2.1"):
        store.add(version, download(installer, packages, version))
        store.set_current(version)
    assert (store.current, store.previous) == ("2.1", "2.0")
    assert sorted(p.name for p in store.versions_dir.iterdir()) == ["2.0", "2.1"]


def test_state_survives_restart(installer, store, packages, factory_dir, signing_key):
    store.bootable(factory_dir)
    store.add("2.0", download(installer, packages, "2.0"))
    store.set_current("2.0")
    again = installer.Store(store.root, signing_key.public_key())
    assert again.bootable(factory_dir).version == "2.0"
    assert again.previous == "1.0"


def test_edited_files_on_disk_are_not_run(installer, store, packages, factory_dir, signing_key):
    """A stored package that no longer verifies falls back to the previous version."""
    store.bootable(factory_dir)
    store.add("2.0", download(installer, packages, "2.0"))
    store.set_current("2.0")
    pkg = store.versions_dir / "2.0" / "firmware.tar.gz"
    pkg.write_bytes(pkg.read_bytes() + b"x")
    again = installer.Store(store.root, signing_key.public_key())
    assert again.bootable(factory_dir).version == "1.0"
    assert not again.has_valid("2.0")


def test_has_valid_checks_the_command_checksum(installer, store, packages, factory_dir):
    store.bootable(factory_dir)
    sha = json.loads(files(packages, "1.0")["manifest.json"])["sha256"]
    assert store.has_valid("1.0", sha)
    assert not store.has_valid("1.0", "0" * 64)
    assert not store.has_valid("2.0")


def test_unverified_download_is_refused(installer, store, packages):
    bad = download(installer, packages, "2.0")
    bad = installer.Download(bad.manifest, bad.signature, bad.package + b"x")
    with pytest.raises(installer.VerificationError, match="package size"):
        store.add("2.0", bad)
    assert not (store.versions_dir / "2.0").exists()


def test_package_with_unsafe_paths_is_refused(installer, release, signing_key, store, tmp_path):
    """Even a correctly signed package cannot write outside its folder."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"print('x')\n"
        info = tarfile.TarInfo("../../escape.py"); info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    package = buf.getvalue()
    manifest = release.build_manifest("3.0", package, "")
    dl = installer.Download(manifest, release.sign(manifest, signing_key), package)
    with pytest.raises(installer.InstallError, match="could not unpack"):
        store.add("3.0", dl)
    assert not (tmp_path / "escape.py").exists()


def test_corrupt_state_file_starts_from_factory(installer, store, factory_dir, signing_key):
    store.root.joinpath("state.json").write_text("{not json")
    again = installer.Store(store.root, signing_key.public_key())
    assert again.bootable(factory_dir).version == "1.0"
