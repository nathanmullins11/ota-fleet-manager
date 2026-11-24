"""Rollout rules with no I/O: building commands, choosing targets, applying device events,
and deciding when a rollout is finished. rollouts.py does the database and MQTT work.

Rollout life cycle (rollouts.state):

  pending      created by cli.py; the orchestrator has not sent commands yet
  in_progress  update commands sent; waiting for device events and health verdicts
  completed    every device failed or updated and was judged healthy
  halted       rollback requested, by an operator or automatically by the health rules
  rolled_back  rollback commands answered by every device

Per device (rollout_devices.state): pending -> commanded -> updated | failed, and after a
rollback request: commanded -> rolled_back | failed.

Health evaluation (FR-4, FR-5): each updated device is watched for a health window and
gets a verdict (rollout_devices.verdict). An unhealthy or unresponsive device, or more
failed installs than allowed, halts the rollout and rolls it back automatically:

  healthy        stayed within the limits for the whole window, proven by a heartbeat
                 sent after the window ended (a device that went quiet partway through
                 is watched until it reports again or is unresponsive)
  unhealthy      more crashes or errors than allowed within the window
  unresponsive   updated, then went silent for longer than allowed (boot loop, broken agent)
  failed_update  reported update_failed (download, verification or start failed)
  no_reply       kept sending heartbeats but never answered the command (agent problem)
  not_reached    sent no heartbeat after the command: offline, not a firmware failure

Only unhealthy, unresponsive, and failed_update or no_reply beyond the allowed number,
trigger a rollback. A device that is merely offline (not_reached) never does.

Staged rollout (FR-2): a rollout can run in stages (`--stages 1,3,6` or `10%,50%,100%`),
devices assigned in device-ID order. Only the current stage's devices are commanded; the
next stage starts once every device in the current one has finished (judged healthy, or
failed within the rollout's limits). A halt rolls back only the devices commanded so far;
devices in later stages are 'skipped' and never receive the update.
"""
import math
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


@dataclass(frozen=True)
class HealthLimits:
    """The limits a rollout is judged by. Defaults come from the orchestrator's settings;
    cli.py can override them per rollout, and they are stored with it."""
    window_seconds: int = 60          # watch each device this long after it updates
    max_crashes: int = 0              # per device, within the window
    max_errors: int = 10              # per device, within the window
    max_failed_devices: int = 0       # failed_update + no_reply allowed before halting
    unresponsive_seconds: int = 45    # silence after updating that counts as unresponsive

    def describe(self) -> str:
        return (f"window {self.window_seconds} s, crashes <= {self.max_crashes}, "
                f"errors <= {self.max_errors}, failed devices <= {self.max_failed_devices}, "
                f"silence <= {self.unresponsive_seconds} s")

    def with_overrides(self, **values) -> "HealthLimits":
        """Replace the fields given (None means keep), checking they are not negative."""
        fields = {k: v for k, v in values.items() if v is not None}
        for key, value in fields.items():
            if type(value) is not int or value < 0:
                raise RolloutError(f"{key} must be a whole number of 0 or more")
        return HealthLimits(**{**self.__dict__, **fields})


@dataclass(frozen=True)
class DeviceHealth:
    """What the orchestrator knows about an updated device in its health window."""
    device_id: str
    version: str                              # the version it updated to
    seconds_since_update: float               # since update_succeeded arrived
    crashes: int                              # summed over heartbeats since then
    errors: int
    seconds_since_heartbeat: Optional[float]  # None: never heard from


BAD_VERDICTS = ("unhealthy", "unresponsive")
FAILED_VERDICTS = ("failed_update", "no_reply")


def _times(n: int) -> str:
    return f"{n} time" if n == 1 else f"{n} times"


def evaluate_device(h: DeviceHealth, limits: HealthLimits) -> Optional[tuple[str, str]]:
    """A device's verdict and its reason, or None while it is still being watched."""
    within = f"within {h.seconds_since_update:.0f} s of updating to {h.version}"
    if h.crashes > limits.max_crashes:
        return "unhealthy", f"crashed {_times(h.crashes)} {within} (limit {limits.max_crashes})"
    if h.errors > limits.max_errors:
        return "unhealthy", f"reported {h.errors} errors {within} (limit {limits.max_errors})"
    silent = h.seconds_since_heartbeat
    if silent is not None and silent > limits.unresponsive_seconds:
        return "unresponsive", (f"no heartbeat for {silent:.0f} s after updating to {h.version} "
                                f"(limit {limits.unresponsive_seconds} s)")
    # Healthy needs a heartbeat from after the window ended: the device lasted the whole
    # window. Silence alone is judged by unresponsive_seconds above, so a device that goes
    # quiet partway through (say 20 s into a 60 s window) is never passed as healthy while
    # its silence is still under the limit; it waits until it reports or times out.
    # A window of 0 (no health watch) only needs the device to have been heard from.
    heard_after_window = silent is not None and (
        limits.window_seconds == 0
        or h.seconds_since_update - silent >= limits.window_seconds)
    if heard_after_window:
        crashes = f"{h.crashes} crash" + ("" if h.crashes == 1 else "es")
        return "healthy", (f"within limits for {limits.window_seconds} s on {h.version}: "
                           f"{crashes}, {h.errors} errors")
    return None


def classify_no_answer(heartbeats_since_command: int, timeout_seconds: int) -> tuple[str, str]:
    """A commanded device that sent no event within the command timeout."""
    if heartbeats_since_command:
        return "no_reply", (f"sent {heartbeats_since_command} heartbeat(s) but no reply to the "
                            f"update command within {timeout_seconds} s")
    return "not_reached", f"no heartbeat or reply within {timeout_seconds} s of the command: offline"


def health_decision(verdicts: dict, limits: HealthLimits) -> Optional[str]:
    """Whether a rollout must halt. verdicts: device_id -> (verdict, reason) for devices that
    have one. Returns the halt reason, or None."""
    for device_id in sorted(verdicts):
        verdict, reason = verdicts[device_id]
        if verdict in BAD_VERDICTS:
            return f"automatic: {device_id} {verdict}: {reason}"
    failed = sorted(d for d, (v, _) in verdicts.items() if v in FAILED_VERDICTS)
    if len(failed) > limits.max_failed_devices:
        details = "; ".join(f"{d} ({verdicts[d][1]})" for d in failed)
        return (f"automatic: {len(failed)} device(s) failed to update "
                f"(limit {limits.max_failed_devices}): {details}")
    return None


def stage_sizes(spec: Optional[str], device_count: int) -> list[int]:
    """Stage sizes for a rollout of device_count devices.

    No spec: one stage. Plain numbers are stage sizes ("1,3,6"); devices left over join the
    last stage, and stages that would be empty are dropped. Percentages are the share of the
    devices reached by the end of each stage ("10%,50%,100%"); the last must be 100%.
    """
    if device_count < 1:
        raise RolloutError("a rollout needs at least one device")
    if not spec or not spec.strip():
        return [device_count]
    parts = [p.strip() for p in spec.split(",")]
    if any(not p for p in parts):
        raise RolloutError(f"stages {spec!r}: empty stage")
    percent = [p.endswith("%") for p in parts]
    try:
        if all(percent):
            shares = [int(p[:-1]) for p in parts]
            if any(not 0 < x <= 100 for x in shares) or any(
                    b <= a for a, b in zip(shares, shares[1:], strict=False)):
                raise ValueError
            if shares[-1] != 100:
                raise RolloutError(f"stages {spec!r}: the last stage must reach 100%")
            reached = [max(1, math.ceil(device_count * x / 100)) for x in shares]
            sizes = [b - a for a, b in zip([0] + reached[:-1], reached, strict=True)]
        elif not any(percent):
            counts = [int(p) for p in parts]
            if any(c < 1 for c in counts):
                raise ValueError
            sizes, left = [], device_count
            for c in counts:
                if left == 0:
                    break
                sizes.append(min(c, left))
                left -= sizes[-1]
            sizes[-1] += left
        else:
            raise RolloutError(f"stages {spec!r}: use either counts (1,3,6) or percentages "
                               "(10%,50%,100%), not both")
    except ValueError:
        raise RolloutError(f"stages {spec!r}: counts must be whole numbers of 1 or more, "
                           "percentages must rise from 1% to 100%") from None
    return [n for n in sizes if n > 0]


def assign_stages(device_ids: list, sizes: list) -> dict:
    """device_id -> stage (1-based), filling stages in device-ID order."""
    ordered = sorted(device_ids)
    if sum(sizes) != len(ordered):
        raise ValueError("stage sizes do not add up to the number of devices")
    stages, i = {}, 0
    for stage, size in enumerate(sizes, start=1):
        for device_id in ordered[i:i + size]:
            stages[device_id] = stage
        i += size
    return stages


def describe_stages(stages: dict) -> str:
    """'stage 1: device-001; stage 2: device-002, device-003; ...' for the decision log."""
    by_stage = {}
    for device_id, stage in sorted(stages.items()):
        by_stage.setdefault(stage, []).append(device_id)
    return "; ".join(f"stage {n}: {', '.join(ids)}" for n, ids in sorted(by_stage.items()))


def stage_finished(devices: list) -> bool:
    """Whether a stage may hand over to the next. devices: (state, verdict) for each device in
    the stage. Finished means every device was judged healthy or failed (within the limits;
    a breach halts the rollout before this is asked)."""
    return bool(devices) and all(
        state == "failed" or (state == "updated" and verdict == "healthy")
        for state, verdict in devices)


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


def outcome(rollout_state: str, device_states: dict,
            verdicts: Optional[dict] = None) -> Optional[tuple[str, str, str]]:
    """Whether a rollout is finished. device_states: device_id -> rollout_devices.state;
    verdicts: device_id -> rollout_devices.verdict (None: health is not evaluated).

    Returns (new rollout state, rollout_events.event, reason), or None if devices are still
    pending or commanded, or updated devices are still in their health window.
    """
    if any(s in ("pending", "commanded") for s in device_states.values()):
        return None
    failed = sorted(d for d, s in device_states.items() if s == "failed")
    total = len(device_states)
    if rollout_state == "in_progress":
        updated = [d for d, s in device_states.items() if s == "updated"]
        if verdicts is not None and any(verdicts.get(d) != "healthy" for d in updated):
            return None                       # still watching (or about to halt)
        reason = (f"{len(updated)} of {total} devices updated"
                  + (" and healthy" if verdicts is not None else ""))
        if failed:
            not_reached = [d for d in failed if (verdicts or {}).get(d) == "not_reached"]
            others = [d for d in failed if d not in not_reached]
            if others:
                reason += f"; failed: {', '.join(others)}"
            if not_reached:
                reason += f"; not reached (offline): {', '.join(not_reached)}"
        return "completed", "completed", reason
    if rollout_state == "halted":
        skipped = sum(s == "skipped" for s in device_states.values())
        restored = sum(s == "rolled_back" for s in device_states.values())
        reason = f"{restored} of {total - skipped} devices rolled back"
        if failed:
            reason += f"; failed: {', '.join(failed)}"
        if skipped:
            reason += f"; {skipped} in later stages never received the update"
        return "rolled_back", "rollback_completed", reason
    return None
