"""Agent settings, read from environment variables."""
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# Same rules as the orchestrator (topics.py, messages.py). Always used with fullmatch()
# and re.ASCII: no trailing newline, digits 0-9 only.
DEVICE_ID_RE = re.compile(r"device-\d{3,}", re.ASCII)
VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?", re.ASCII)

HERE = Path(__file__).resolve().parent


def origin(url: str) -> Optional[tuple]:
    """(scheme, host, port) of an http(s) URL, with the default port filled in; None if the
    URL has no usable origin."""
    try:
        parsed = urlparse(url)
        port = parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname or port is None:
        return None
    return parsed.scheme, parsed.hostname.lower(), port


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
    # Broker sign-in and TLS (FR-7). The username defaults to the device ID; the password is
    # this device's own (derived from DEVICE_SECRET by the launcher). mqtt_ca_cert: the CA
    # that signed the broker's certificate; empty means plain text (tests only).
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_ca_cert: str = ""
    # The software registry. Commands whose download URLs point anywhere else are refused
    # (threat S4). load_config() always sets it; empty (tests only) allows any host.
    registry_url: str = ""


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

    mqtt_host = os.environ.get("MQTT_HOST", "localhost")
    # The registry runs on the control-plane host next to the broker, on port 8080
    # (deploy/docker-compose.yml), so that is the default.
    registry_url = os.environ.get("REGISTRY_URL") or f"http://{mqtt_host}:8080"
    if origin(registry_url) is None:
        raise SystemExit(f"REGISTRY_URL must be an http(s) URL (got {registry_url!r})")

    return Config(
        device_id=device_id,
        mqtt_host=mqtt_host,
        mqtt_port=port,
        heartbeat_interval_seconds=_positive_float("HEARTBEAT_INTERVAL_SECONDS", "5"),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        firmware_dir=Path(os.environ.get("FIRMWARE_DIR", Path.home() / "firmware")),
        factory_dir=Path(os.environ.get("FACTORY_DIR", HERE / "factory")),
        public_key_path=Path(os.environ.get("RELEASE_PUBLIC_KEY", HERE / "release-public.pem")),
        start_timeout_seconds=_positive_float("FIRMWARE_START_TIMEOUT_SECONDS", "5"),
        restart_delay_seconds=_positive_float("FIRMWARE_RESTART_DELAY_SECONDS", "2"),
        download_timeout_seconds=_positive_float("DOWNLOAD_TIMEOUT_SECONDS", "15"),
        mqtt_username=os.environ.get("MQTT_USERNAME", device_id),
        mqtt_password=os.environ.get("MQTT_PASSWORD", ""),
        mqtt_ca_cert=os.environ.get("MQTT_CA_CERT", ""),
        registry_url=registry_url.rstrip("/"),
    )
