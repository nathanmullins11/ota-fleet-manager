"""Device agent: parsing update and rollback commands (mqtt-topics-and-messages.md 4.3, 4.4)."""
import json

import pytest

pytestmark = pytest.mark.unit

SHA = "ab" * 32


def command(**changes) -> dict:
    cmd = {
        "schema_version": 1, "command": "update", "rollout_id": 7, "target_version": "2.0",
        "download_url": "http://203.0.113.10:8080/packages/2.0",
        "manifest_url": "http://203.0.113.10:8080/packages/2.0/manifest",
        "signature_url": "http://203.0.113.10:8080/packages/2.0/manifest.sig",
        "sha256": SHA, "issued_at": "2026-10-29T14:05:30Z",
    }
    cmd.update(changes)
    return {k: v for k, v in cmd.items() if v is not ...}


def parse(agent_commands, cmd) -> object:
    return agent_commands.parse_command(json.dumps(cmd).encode())


@pytest.mark.parametrize("kind", ["update", "rollback"])
def test_valid_command(agent_commands, kind):
    cmd = parse(agent_commands, command(command=kind))
    assert (cmd.command, cmd.rollout_id, cmd.target_version, cmd.sha256) == (kind, 7, "2.0", SHA)
    assert cmd.failed_event == f"{kind}_failed"


def test_orchestrator_commands_are_accepted(agent_commands, rollout_logic):
    """What the orchestrator builds, the agent must accept."""
    for kind in ("update", "rollback"):
        built = rollout_logic.build_command(kind, 3, "2.1", SHA, "http://10.0.0.5:8080/")
        cmd = parse(agent_commands, built)
        assert cmd.download_url == "http://10.0.0.5:8080/packages/2.1"


def test_unknown_fields_are_ignored(agent_commands):
    assert parse(agent_commands, command(extra="x")).target_version == "2.0"


@pytest.mark.parametrize("changes,reason", [
    ({"schema_version": 2}, "schema_version"),
    ({"schema_version": True}, "schema_version"),
    ({"download_url": "/packages/2.0"}, "download_url"),
    ({"manifest_url": "ftp://host/x"}, "manifest_url"),
    ({"signature_url": ...}, "signature_url"),
    ({"sha256": "AB" * 32}, "sha256"),
    ({"sha256": "ab" * 31}, "sha256"),
])
def test_bad_command_is_answerable(agent_commands, changes, reason):
    """The rollout and version are known, so the agent can still answer with a failure event."""
    with pytest.raises(agent_commands.InvalidCommand, match=reason) as err:
        parse(agent_commands, command(**changes))
    assert err.value.answerable
    assert (err.value.command, err.value.rollout_id, err.value.version) == ("update", 7, "2.0")


@pytest.mark.parametrize("changes", [
    {"command": "reboot"}, {"rollout_id": 0}, {"rollout_id": "7"}, {"rollout_id": True},
    {"target_version": "2.0\n"}, {"target_version": ...},
])
def test_unidentifiable_command_is_not_answerable(agent_commands, changes):
    with pytest.raises(agent_commands.InvalidCommand) as err:
        parse(agent_commands, command(**changes))
    assert not err.value.answerable


@pytest.mark.parametrize("payload", [b"not json", b"[1]", b"\xff"])
def test_garbage_payload(agent_commands, payload):
    with pytest.raises(agent_commands.InvalidCommand):
        agent_commands.parse_command(payload)
