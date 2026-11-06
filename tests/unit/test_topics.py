"""Topic parsing (deploy/orchestrator/topics.py)."""
import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("topic, expected", [
    ("ota/devices/device-001/heartbeat", ("device-001", "heartbeat")),
    ("ota/devices/device-1234/events", ("device-1234", "events")),
])
def test_valid_topics(topics, topic, expected):
    assert topics.parse_device_topic(topic) == expected


@pytest.mark.parametrize("topic", [
    "ota/devices/device-001\n/heartbeat",       # defect 1: trailing newline in the ID
    "ota/devices/device-١٢٣/heartbeat",  # defect 2: Arabic-Indic digits
    "ota/devices/device-００１/heartbeat",  # defect 2: full-width digits
    "ota/devices/device-01/heartbeat",          # too few digits
    "ota/devices/Device-001/heartbeat",
    "ota/devices/device-001/heartbeat/extra",
    "ota/devices/heartbeat",
    "fleet/devices/device-001/heartbeat",
    "",
])
def test_invalid_topics(topics, topic):
    assert topics.parse_device_topic(topic) is None
