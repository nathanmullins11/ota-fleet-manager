"""Parse and validate device messages against docs/mqtt-topics-and-messages.md.

A bad message raises InvalidMessage with a short reason. Callers log it and drop it;
a bad message must never crash the orchestrator.
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

SUPPORTED_SCHEMA_VERSION = 1
# 'offline' and 'unknown' are assigned by the orchestrator; devices never send them.
DEVICE_STATUSES = {"healthy", "degraded", "unhealthy", "updating"}
EVENT_NAMES = {
    "update_started", "update_succeeded", "update_failed",
    "rollback_started", "rollback_succeeded", "rollback_failed",
}
# Always used with fullmatch(): re.match() with "$" would also accept a trailing newline.
# re.ASCII keeps \d to 0-9 (otherwise it matches digits from any script).
VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?", re.ASCII)

# Upper bounds of the database columns these fields are stored in (init.sql).
INT4_MAX = 2**31 - 1    # INTEGER: devices.error_count, heartbeats.error_count
INT8_MAX = 2**63 - 1    # BIGINT:  rollouts.id (rollout_id)


class InvalidMessage(ValueError):
    pass


@dataclass(frozen=True)
class Heartbeat:
    device_id: str
    version: str
    status: str
    error_count: int
    timestamp: datetime


@dataclass(frozen=True)
class Event:
    device_id: str
    event: str
    rollout_id: int
    version: str
    reason: Optional[str]
    timestamp: datetime


def _load(payload: bytes, topic_device_id: str) -> dict:
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise InvalidMessage("payload is not valid JSON") from None
    if not isinstance(data, dict):
        raise InvalidMessage("payload is not a JSON object")
    schema_version = data.get("schema_version")
    # Exact type check: 1.0 and true both compare equal to 1 but are not the integer 1.
    if type(schema_version) is not int or schema_version != SUPPORTED_SCHEMA_VERSION:
        raise InvalidMessage(f"unsupported schema_version {schema_version!r}")
    if data.get("device_id") != topic_device_id:
        raise InvalidMessage(
            f"device_id {data.get('device_id')!r} does not match topic ({topic_device_id})")
    return data


def _int(data: dict, key: str, minimum: int = 0, maximum: int = INT4_MAX) -> int:
    """An integer in [minimum, maximum]; the maximum keeps it within its database column."""
    value = data.get(key)
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidMessage(f"{key} must be an integer from {minimum} to {maximum}")
    return value


def _version(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise InvalidMessage(f"{key} must look like '1.0' or '2.1.3'")
    return value


def _timestamp(data: dict) -> datetime:
    value = data.get("timestamp")
    if not isinstance(value, str):
        raise InvalidMessage("timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidMessage("timestamp is not ISO 8601") from None
    if parsed.tzinfo is None:
        raise InvalidMessage("timestamp must include a time zone (use a trailing Z)")
    # The contract requires UTC. "Z" and "+00:00" are both UTC; "+05:00" is not.
    if parsed.utcoffset() != timedelta(0):
        raise InvalidMessage("timestamp must be in UTC (use a trailing Z)")
    return parsed


def parse_heartbeat(topic_device_id: str, payload: bytes) -> Heartbeat:
    data = _load(payload, topic_device_id)
    status = data.get("status")
    if status not in DEVICE_STATUSES:
        raise InvalidMessage(f"status must be one of {sorted(DEVICE_STATUSES)}")
    return Heartbeat(
        device_id=topic_device_id,
        version=_version(data, "version"),
        status=status,
        error_count=_int(data, "error_count"),
        timestamp=_timestamp(data),
    )


def parse_event(topic_device_id: str, payload: bytes) -> Event:
    data = _load(payload, topic_device_id)
    name = data.get("event")
    if name not in EVENT_NAMES:
        raise InvalidMessage(f"event must be one of {sorted(EVENT_NAMES)}")
    reason = data.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise InvalidMessage("reason must be a string or null")
    return Event(
        device_id=topic_device_id,
        event=name,
        rollout_id=_int(data, "rollout_id", minimum=1, maximum=INT8_MAX),
        version=_version(data, "version"),
        reason=reason,
        timestamp=_timestamp(data),
    )
