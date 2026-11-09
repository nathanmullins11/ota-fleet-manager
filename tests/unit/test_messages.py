"""Orchestrator message validation (deploy/orchestrator/messages.py)."""
import json
from datetime import timezone

import pytest

pytestmark = pytest.mark.unit

DEVICE = "device-001"


def heartbeat(**overrides) -> bytes:
    """The contract's example heartbeat, with fields overridden (None removes a field)."""
    msg = {
        "schema_version": 1,
        "device_id": DEVICE,
        "version": "1.0",
        "status": "healthy",
        "error_count": 0,
        "crash_count": 0,
        "timestamp": "2026-10-29T14:03:07Z",
    }
    for key, value in overrides.items():
        if value is None:
            msg.pop(key, None)
        else:
            msg[key] = value
    return json.dumps(msg).encode()


def event(**overrides) -> bytes:
    msg = {
        "schema_version": 1,
        "device_id": DEVICE,
        "event": "update_failed",
        "rollout_id": 7,
        "version": "2.1",
        "reason": "checksum mismatch",
        "timestamp": "2026-10-29T14:03:07Z",
    }
    msg.update(overrides)
    return json.dumps(msg).encode()


def test_contract_example_is_accepted(messages):
    hb = messages.parse_heartbeat(DEVICE, heartbeat())
    assert (hb.device_id, hb.version, hb.status, hb.error_count) == (DEVICE, "1.0", "healthy", 0)
    assert hb.timestamp.utcoffset().total_seconds() == 0


@pytest.mark.parametrize("version", ["1.0", "2.1", "2.1.3", "10.20.30"])
def test_valid_versions_accepted(messages, version):
    assert messages.parse_heartbeat(DEVICE, heartbeat(version=version)).version == version


# Defect 1 and 2: trailing newline, other whitespace, non-ASCII digits.
@pytest.mark.parametrize("version", [
    "1.0\n", " 1.0", "1.0 ", "1", "1.", "v1.0", "1.0.0.0", "1.0-beta",
    "١.٠",   # Arabic-Indic digits
    "１.０",   # full-width digits
])
def test_invalid_versions_rejected(messages, version):
    with pytest.raises(messages.InvalidMessage, match="version must look like"):
        messages.parse_heartbeat(DEVICE, heartbeat(version=version))


# Defect 3: error_count must fit the INTEGER column.
@pytest.mark.parametrize("value", [0, 1, 2**31 - 1])
def test_error_count_in_range_accepted(messages, value):
    assert messages.parse_heartbeat(DEVICE, heartbeat(error_count=value)).error_count == value


@pytest.mark.parametrize("value", [-1, 2**31, 10**30, True, False, 1.0, 0.5, "0"])
def test_error_count_out_of_range_or_wrong_type_rejected(messages, value):
    with pytest.raises(messages.InvalidMessage, match="error_count must be an integer"):
        messages.parse_heartbeat(DEVICE, heartbeat(error_count=value))


# crash_count: optional, same range rules as error_count.
@pytest.mark.parametrize("value", [0, 1, 2**31 - 1])
def test_crash_count_in_range_accepted(messages, value):
    assert messages.parse_heartbeat(DEVICE, heartbeat(crash_count=value)).crash_count == value


def test_crash_count_defaults_to_zero_when_absent(messages):
    # Agents that predate crash reporting must stay valid.
    hb = messages.parse_heartbeat(DEVICE, heartbeat(crash_count=None))
    assert hb.crash_count == 0


@pytest.mark.parametrize("value", [-1, 2**31, True, 1.0, "0"])
def test_crash_count_out_of_range_or_wrong_type_rejected(messages, value):
    with pytest.raises(messages.InvalidMessage, match="crash_count must be an integer"):
        messages.parse_heartbeat(DEVICE, heartbeat(crash_count=value))


def test_crash_count_null_rejected(messages):
    msg = json.loads(heartbeat())
    msg["crash_count"] = None
    with pytest.raises(messages.InvalidMessage, match="crash_count must be an integer"):
        messages.parse_heartbeat(DEVICE, json.dumps(msg).encode())


def test_crash_count_kept_apart_from_error_count(messages):
    hb = messages.parse_heartbeat(DEVICE, heartbeat(error_count=25, crash_count=1))
    assert (hb.error_count, hb.crash_count) == (25, 1)


# Defect 4: schema_version must be the integer 1, not something equal to it.
@pytest.mark.parametrize("value", [1.0, True, "1", 2, 0])
def test_schema_version_must_be_integer_one(messages, value):
    with pytest.raises(messages.InvalidMessage, match="unsupported schema_version"):
        messages.parse_heartbeat(DEVICE, heartbeat(schema_version=value))


def test_schema_version_missing_rejected(messages):
    with pytest.raises(messages.InvalidMessage, match="unsupported schema_version"):
        messages.parse_heartbeat(DEVICE, heartbeat(schema_version=None))


# Defect 5: UTC only.
@pytest.mark.parametrize("ts", ["2026-10-29T14:03:07Z", "2026-10-29T14:03:07+00:00",
                                "2026-10-29T14:03:07.123456Z"])
def test_utc_timestamps_accepted(messages, ts):
    hb = messages.parse_heartbeat(DEVICE, heartbeat(timestamp=ts))
    assert hb.timestamp.utcoffset().total_seconds() == 0
    assert hb.timestamp.astimezone(timezone.utc).hour == 14


@pytest.mark.parametrize("ts", ["2026-10-29T14:03:07+05:00", "2026-10-29T14:03:07-04:00",
                                "2026-10-29T14:03:07+00:01"])
def test_non_utc_offsets_rejected(messages, ts):
    with pytest.raises(messages.InvalidMessage, match="timestamp must be in UTC"):
        messages.parse_heartbeat(DEVICE, heartbeat(timestamp=ts))


def test_naive_timestamp_still_rejected(messages):
    with pytest.raises(messages.InvalidMessage, match="must include a time zone"):
        messages.parse_heartbeat(DEVICE, heartbeat(timestamp="2026-10-29T14:03:07"))


# Existing behaviour the fixes must not break.
@pytest.mark.parametrize("overrides, reason", [
    ({"status": "offline"}, "status must be one of"),
    ({"status": None}, "status must be one of"),
    ({"device_id": "device-002"}, "does not match topic"),
    ({"timestamp": "yesterday"}, "not ISO 8601"),
    ({"timestamp": None}, "timestamp is missing"),
])
def test_existing_rejections_unchanged(messages, overrides, reason):
    with pytest.raises(messages.InvalidMessage, match=reason):
        messages.parse_heartbeat(DEVICE, heartbeat(**overrides))


@pytest.mark.parametrize("payload", [b"", b"not json", b"[]", b"\xff\xfe", b"null"])
def test_non_object_payloads_rejected(messages, payload):
    with pytest.raises(messages.InvalidMessage):
        messages.parse_heartbeat(DEVICE, payload)


def test_event_accepted(messages):
    ev = messages.parse_event(DEVICE, event())
    assert (ev.event, ev.rollout_id, ev.version) == ("update_failed", 7, "2.1")


@pytest.mark.parametrize("rollout_id", [1, 2**63 - 1])
def test_event_rollout_id_in_bigint_range(messages, rollout_id):
    assert messages.parse_event(DEVICE, event(rollout_id=rollout_id)).rollout_id == rollout_id


@pytest.mark.parametrize("rollout_id", [0, 2**63, True, 1.0])
def test_event_rollout_id_out_of_range_rejected(messages, rollout_id):
    with pytest.raises(messages.InvalidMessage, match="rollout_id must be an integer"):
        messages.parse_event(DEVICE, event(rollout_id=rollout_id))
