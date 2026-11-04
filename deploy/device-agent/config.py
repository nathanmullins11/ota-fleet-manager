"""Agent settings, read from environment variables."""
import os
import re
from dataclasses import dataclass

# Same rules as the orchestrator (topics.py, messages.py). Always used with fullmatch()
# and re.ASCII: no trailing newline, digits 0-9 only.
DEVICE_ID_RE = re.compile(r"device-\d{3,}", re.ASCII)
VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?", re.ASCII)


@dataclass(frozen=True)
class Config:
    device_id: str                    # also used as the MQTT client ID (must be unique)
    mqtt_host: str
    mqtt_port: int
    firmware_version: str             # from the environment for now; from the installed package in Iteration 2
    heartbeat_interval_seconds: float
    log_level: str


def load_config() -> Config:
    device_id = os.environ.get("DEVICE_ID", "")
    if not DEVICE_ID_RE.fullmatch(device_id):
        raise SystemExit(f"DEVICE_ID must look like 'device-001' (got {device_id!r})")

    version = os.environ.get("FIRMWARE_VERSION", "1.0")
    if not VERSION_RE.fullmatch(version):
        raise SystemExit(f"FIRMWARE_VERSION must look like '1.0' (got {version!r})")

    try:
        interval = float(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "5"))
        port = int(os.environ.get("MQTT_PORT", "1883"))
    except ValueError as exc:
        raise SystemExit(f"Invalid numeric setting: {exc}") from None
    if interval <= 0:
        raise SystemExit("HEARTBEAT_INTERVAL_SECONDS must be greater than 0")

    return Config(
        device_id=device_id,
        mqtt_host=os.environ.get("MQTT_HOST", "localhost"),
        mqtt_port=port,
        firmware_version=version,
        heartbeat_interval_seconds=interval,
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )