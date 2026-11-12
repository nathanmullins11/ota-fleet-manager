"""Agent settings, read from environment variables."""
import os
import re
from dataclasses import dataclass
from pathlib import Path

# Same rules as the orchestrator (topics.py, messages.py). Always used with fullmatch()
# and re.ASCII: no trailing newline, digits 0-9 only.
DEVICE_ID_RE = re.compile(r"device-\d{3,}", re.ASCII)
VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?", re.ASCII)

HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Config:
    device_id: str                    # also used as the MQTT client ID (must be unique)
    mqtt_host: str
    mqtt_port: int
    heartbeat_interval_seconds: float
    log_level: str
    firmware_dir: Path                # installed versions and state.json (writable)
    factory_dir: Path                 # signed package the device ships with: factory/<version>/
    public_key_path: Path             # release public key that packages must be signed with
    start_timeout_seconds: float      # new firmware must report a status within this time
    restart_delay_seconds: float      # wait before restarting crashed firmware
    download_timeout_seconds: float


def _positive_float(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number (got {raw!r})") from None
    if value <= 0:
        raise SystemExit(f"{name} must be greater than 0")
    return value


def load_config() -> Config:
    device_id = os.environ.get("DEVICE_ID", "")
    if not DEVICE_ID_RE.fullmatch(device_id):
        raise SystemExit(f"DEVICE_ID must look like 'device-001' (got {device_id!r})")

    try:
        port = int(os.environ.get("MQTT_PORT", "1883"))
    except ValueError as exc:
        raise SystemExit(f"Invalid numeric setting: {exc}") from None

    return Config(
        device_id=device_id,
        mqtt_host=os.environ.get("MQTT_HOST", "localhost"),
        mqtt_port=port,
        heartbeat_interval_seconds=_positive_float("HEARTBEAT_INTERVAL_SECONDS", "5"),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        firmware_dir=Path(os.environ.get("FIRMWARE_DIR", Path.home() / "firmware")),
        factory_dir=Path(os.environ.get("FACTORY_DIR", HERE / "factory")),
        public_key_path=Path(os.environ.get("RELEASE_PUBLIC_KEY", HERE / "release-public.pem")),
        start_timeout_seconds=_positive_float("FIRMWARE_START_TIMEOUT_SECONDS", "5"),
        restart_delay_seconds=_positive_float("FIRMWARE_RESTART_DELAY_SECONDS", "2"),
        download_timeout_seconds=_positive_float("DOWNLOAD_TIMEOUT_SECONDS", "15"),
    )
