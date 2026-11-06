"""Rollout rules with no I/O: building commands, choosing targets, applying device events,
and deciding when a rollout is finished. rollouts.py does the database and MQTT work.

Rollout life cycle (rollouts.state):

  pending      created by cli.py; the orchestrator has not sent commands yet
  in_progress  update commands sent; waiting for device events
  completed    every device answered (updated or failed); nothing more to send
  halted       rollback requested (manually now, automatically in Iteration 3)
  rolled_back  rollback commands answered by every device

Per device (rollout_devices.state): pending -> commanded -> updated | failed, and after a
rollback request: commanded -> rolled_back | failed.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

COMMAND_SCHEMA_VERSION = 1
ACTIVE_STATES = ("pending", "in_progress", "halted")
ROLLBACK_FROM_STATES = ("in_progress", "completed")

# Device event -> new rollout_devices.state, per phase. None means "no change".
UPDATE_PHASE_EVENTS = {
    "update_started": None,          # already 'commanded'
    "update_succeeded": "updated",
    "update_failed": "failed",
}
ROLLBACK_PHASE_EVENTS = {
    "rollback_started": None,
    "rollback_succeeded": "rolled_back",
    "rollback_failed": "failed",
}


class RolloutError(Exception):
    """A request that cannot be carried out; the message says why."""


@dataclass(frozen=True)
class DeviceRow:
    device_id: str
    current_version: Optional[str]
    status: str


@dataclass(frozen=True)
class TargetPlan:
    to_command: list            # device IDs that get an update command
    already_on_target: list     # running the target version already: no command
    skipped_offline: list       # offline or never heard from: a command would be lost
    rollback_version: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_command(command: str, rollout_id: int, version: str, sha256: str,
                  public_url: str, issued_at: Optional[str] = None) -> dict:
    """An update or rollback command as defined in mqtt-topics-and-messages.md 4.3 and 4.4."""
    if command not in ("update", "rollback"):
        raise ValueError(f"unknown command {command!r}")
    package = f"{public_url.rstrip('/')}/packages/{version}"
    return {
        "schema_version": COMMAND_SCHEMA_VERSION,
        "command": command,
        "rollout_id": rollout_id,
        "target_version": version,
        "download_url": package,
        "manifest_url": f"{package}/manifest",
        "signature_url": f"{package}/manifest.sig",
        "sha256": sha256,
        "issued_at": issued_at or utc_now(),
    }


def plan_targets(devices: list, target_version: str, requested: Optional[list],
                 rollback_version: Optional[str]) -> TargetPlan:
    """Choose which devices get the update.

    devices: every DeviceRow in the registry. requested: device IDs, or None for all.
    rollback_version: given by the operator, or None to use the version the commanded
    devices run now (they must all run the same one).
    """
    known = {d.device_id: d for d in devices}
    if requested is not None:
        unknown = sorted(set(requested) - set(known))
        if unknown:
            raise RolloutError(f"unknown devices: {', '.join(unknown)}")
        candidates = [known[i] for i in sorted(set(requested))]
    else:
        candidates = sorted(devices, key=lambda d: d.device_id)
    if not candidates:
        raise RolloutError("no devices in the registry")

    to_command, on_target, offline = [], [], []
    for d in candidates:
        if d.status in ("offline", "unknown"):
            offline.append(d.device_id)
        elif d.current_version == target_version:
            on_target.append(d.device_id)
        else:
            to_command.append(d)
    if not to_command:
        raise RolloutError(
            f"nothing to do: {len(on_target)} device(s) already on {target_version}, "
            f"{len(offline)} offline")

    if rollback_version is None:
        versions = Counter(d.current_version for d in to_command)
        if len(versions) > 1:
            listed = ", ".join(f"{v} ({n})" for v, n in sorted(versions.items(), key=str))
            raise RolloutError(f"target devices run different versions ({listed}); "
                               "choose the rollback version with --rollback-to")
        rollback_version = next(iter(versions))
        if rollback_version is None:
            raise RolloutError("target devices have not reported a version; use --rollback-to")
    if rollback_version == target_version:
        raise RolloutError("rollback version must differ from the target version")

    return TargetPlan([d.device_id for d in to_command], on_target, offline, rollback_version)


def check_can_start(active_rollouts: list) -> None:
    """active_rollouts: (id, state) pairs in ACTIVE_STATES."""
    if active_rollouts:
        rid, state = active_rollouts[0]
        raise RolloutError(f"rollout {rid} is still {state}; only one rollout runs at a time")


def check_can_roll_back(state: str, rollout_id: int) -> None:
    if state not in ROLLBACK_FROM_STATES:
        raise RolloutError(
            f"rollout {rollout_id} is {state}; only in_progress or completed rollouts can be "
            "rolled back")


def apply_event(rollout_state: str, event: str, event_version: str, target_version: str,
                rollback_version: str) -> tuple[Optional[str], Optional[str]]:
    """What a device event does to its rollout_devices row.

    Returns (new_state, ignore_reason): new_state None means no change; ignore_reason is
    set when the event does not belong to the rollout's current phase.
    """
    if rollout_state in ("in_progress", "completed"):
        table, expected = UPDATE_PHASE_EVENTS, target_version
    elif rollout_state in ("halted", "rolled_back"):
        table, expected = ROLLBACK_PHASE_EVENTS, rollback_version
    else:
        return None, f"rollout is {rollout_state}"
    if event not in table:
        return None, f"{event} does not apply while the rollout is {rollout_state}"
    if event_version != expected:
        return None, f"event is for version {event_version}, expected {expected}"
    return table[event], None


def outcome(rollout_state: str, device_states: dict) -> Optional[tuple[str, str, str]]:
    """Whether a rollout is finished. device_states: device_id -> rollout_devices.state.

    Returns (new rollout state, rollout_events.event, reason), or None if devices are still
    pending or commanded.
    """
    if any(s in ("pending", "commanded") for s in device_states.values()):
        return None
    failed = sorted(d for d, s in device_states.items() if s == "failed")
    total = len(device_states)
    if rollout_state == "in_progress":
        updated = sum(s == "updated" for s in device_states.values())
        reason = f"{updated} of {total} devices updated"
        if failed:
            reason += f"; failed: {', '.join(failed)}"
        return "completed", "completed", reason
    if rollout_state == "halted":
        restored = sum(s == "rolled_back" for s in device_states.values())
        reason = f"{restored} of {total} devices rolled back"
        if failed:
            reason += f"; failed: {', '.join(failed)}"
        return "rolled_back", "rollback_completed", reason
    return None
