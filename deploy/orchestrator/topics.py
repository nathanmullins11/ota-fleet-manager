"""Topic names. Single source of truth; see docs/mqtt-topics-and-messages.md."""
import re

HEARTBEAT_SUBSCRIPTION = "ota/devices/+/heartbeat"
EVENTS_SUBSCRIPTION = "ota/devices/+/events"

DEVICE_ID_RE = re.compile(r"^device-\d{3,}$")


def command_topic(device_id: str) -> str:
    """Topic a single device listens on for commands (used from Iteration 2)."""
    return f"ota/devices/{device_id}/commands"


def parse_device_topic(topic: str):
    """Split 'ota/devices/<id>/<kind>' into (device_id, kind), or None if it doesn't fit."""
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != "ota" or parts[1] != "devices":
        return None
    device_id, kind = parts[2], parts[3]
    if not DEVICE_ID_RE.match(device_id):
        return None
    return device_id, kind
