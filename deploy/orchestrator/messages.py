"""Parse and validate device messages against docs/mqtt-topics-and-messages.md.

A bad message raises InvalidMessage with a short reason. Callers log it and drop it;
a bad message must never crash the orchestrator.
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

SUPPORTED_SCHEMA_VERSION = 1
# 'offline' and 'unknown' are assigned by the orchestrator; devices never send them.
DEVICE_STATUSES = {"healthy", "degraded", "unhealthy", "updating"}
EVENT_NAMES = {
    "update_started", "update_succeeded", "update_failed",
    "rollback_started", "rollback_succeeded", "rollback_failed",
}
VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")


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
    if data.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise InvalidMessage(f"unsupported schema_version {data.get('schema_version')!r}")
    if data.get("device_id") != topic_device_id:
        raise InvalidMessage(
            f"device_id {data.get('device_id')!r} does not match topic ({topic_device_id})")
    return data


def _int(data: dict, key: str, minimum: int = 0) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InvalidMessage(f"{key} must be an integer >= {minimum}")
    return value


def _version(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not VERSION_RE.match(value):
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
        rollout_id=_int(data, "rollout_id", minimum=1),
        version=_version(data, "version"),
        reason=reason,
        timestamp=_timestamp(data),
    )
