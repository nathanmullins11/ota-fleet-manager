"""Settings, read from environment variables (see deploy/.env.example)."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    mqtt_host: str
    mqtt_port: int
    mqtt_client_id: str
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    heartbeat_timeout_seconds: int   # no heartbeat for this long -> device marked offline
    offline_check_interval_seconds: int
    log_level: str


def load_config() -> Config:
    try:
        password = os.environ["POSTGRES_PASSWORD"]
    except KeyError:
        raise SystemExit("POSTGRES_PASSWORD is not set") from None
    return Config(
        mqtt_host=os.environ.get("MQTT_HOST", "localhost"),
        mqtt_port=int(os.environ.get("MQTT_PORT", "1883")),
        mqtt_client_id=os.environ.get("MQTT_CLIENT_ID", "orchestrator"),
        db_host=os.environ.get("DB_HOST", "localhost"),
        db_port=int(os.environ.get("DB_PORT", "5432")),
        db_name=os.environ.get("POSTGRES_DB", "ota"),
        db_user=os.environ.get("POSTGRES_USER", "ota"),
        db_password=password,
        heartbeat_timeout_seconds=int(os.environ.get("HEARTBEAT_TIMEOUT_SECONDS", "30")),
        offline_check_interval_seconds=int(os.environ.get("OFFLINE_CHECK_INTERVAL_SECONDS", "5")),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
