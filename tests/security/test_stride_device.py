"""STRIDE threats against a real device: agent.Device, real firmware processes, HTTP servers.

The attacker can publish any command (the broker is anonymous, S1) and can run a web
server. They do not have the release private key. IDs refer to docs/threat-model.md.
"""
import pytest

from .stride import open_gap

pytestmark = [pytest.mark.component, pytest.mark.security]


def outcome(device):
    """The last event's name and reason."""
    name, _rid, _version, reason = device.events[-1]
    return name, reason


# =========================================================================== Tampering

def test_T2_package_signed_by_another_key_is_not_installed(device, command, attacker, release,
                                                          tmp_path):
    """The attacker builds firmware "2.0" with their own key and commands the device to it."""
    attacker_key = release.Ed25519PrivateKey.generate()
    out = release.build("2.0", release.FIRMWARE_DIR / "2.0", tmp_path / "evil", attacker_key)
    attacker.publish("2.0", out)
    device.handle(command(attacker, "update", "2.0"))
    assert outcome(device) == ("update_failed", "signature verification failed")
    assert device.heartbeat()["version"] == "1.0"
    assert "/packages/2.0" not in attacker.requests          # package itself never fetched
    assert not (device.store.versions_dir / "2.0").exists()


def test_T2_genuine_manifest_replayed_for_another_version_is_refused(device, command, attacker,
                                                                    packages):
    """Old, genuinely signed files served under a newer version's name."""
    attacker.publish("2.1", packages / "1.0")
    device.handle(command(attacker, "update", "2.1"))
    assert outcome(device) == ("update_failed", "manifest is for version '1.0', expected '2.1'")
    assert device.heartbeat()["version"] == "1.0"


def test_T2_genuine_manifest_with_swapped_package_is_refused(device, command, attacker, packages):
    attacker.publish("2.0", packages / "2.0", package=(packages / "2.1" / "firmware.tar.gz").read_bytes())
    device.handle(command(attacker, "update", "2.0"))
    name, reason = outcome(device)
    assert name == "update_failed" and reason in ("package size does not match manifest",
                                                  "checksum mismatch")
    assert device.runner.version == "1.0"


def test_T2_edited_package_on_disk_is_not_used_for_rollback(device, command, registry):
    """A modified kept copy of 1.0 fails re-verification; the device downloads a fresh one."""
    device.handle(command(registry, "update", "2.0"))
    kept = device.store.versions_dir / "1.0" / "firmware.tar.gz"
    kept.write_bytes(kept.read_bytes() + b"\0")
    registry.requests.clear()
    device.handle(command(registry, "rollback", "1.0"))
    assert outcome(device) == ("rollback_succeeded", None)
    assert "/packages/1.0" in registry.requests                # fetched again, verified
    assert device.store.verify_local("1.0")["version"] == "1.0"


@open_gap("T3", "the unpacked app/ folder is not re-verified before it runs; firmware runs as the "
                "agent's user and can edit the kept previous version, which then runs on rollback")
def test_T3_edited_unpacked_firmware_never_runs(device, command, registry, tmp_path):
    device.handle(command(registry, "update", "2.0"))
    marker = tmp_path / "planted-code-ran"
    main_py = device.store.versions_dir / "1.0" / "app" / "main.py"
    main_py.write_text(f"open({str(marker)!r}, 'w').write('x')\n" + main_py.read_text())
    device.handle(command(registry, "rollback", "1.0"))
    assert not marker.exists()


@open_gap("T4", "no anti-rollback: a forged 'update' to an older signed version is installed")
def test_T4_update_command_cannot_downgrade(device, command, registry):
    device.handle(command(registry, "update", "2.0"))
    device.handle(command(registry, "update", "1.0", rollout_id=8))   # forged, not a rollback
    assert outcome(device)[0] == "update_failed"
    assert device.runner.version == "2.0"


# ==================================================================== Spoofing / disclosure

@open_gap("S4", "devices download from any host named in a command, not only the registry")
def test_S4_device_only_contacts_the_registry(device, command, attacker, packages):
    attacker.publish("2.0", packages / "2.0")          # genuine files, attacker's server
    device.handle(command(attacker, "update", "2.0"))
    assert attacker.requests == []


# =========================================================================== Denial of service

def test_D4_repeated_command_does_no_work(device, command, registry):
    """Replaying a command for the running version costs no download and no restart."""
    device.handle(command(registry, "update", "2.0"))
    registry.requests.clear()
    runs = device.runner._run                          # increments on every firmware start
    for rid in range(10, 15):
        device.handle(command(registry, "update", "2.0", rollout_id=rid))
    assert registry.requests == []
    assert outcome(device) == ("update_succeeded", "already running 2.0")
    assert device.runner._run == runs


def test_D4_firmware_that_does_not_start_is_backed_out(device, command, registry):
    """A signed but broken version (9.0 exits at once) cannot take the device down."""
    device.handle(command(registry, "update", "9.0"))
    name, reason = outcome(device)
    assert name == "update_failed" and reason.endswith("still running 1.0")
    assert device.runner.version == "1.0" and device.store.current == "1.0"
