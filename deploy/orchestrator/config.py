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
    registry_url: str                # how the orchestrator reaches the software registry
    registry_public_url: str         # how devices reach it; goes into command URLs
    command_timeout_seconds: int     # no event from a commanded device for this long -> failed
    log_level: str
    # Default health limits for rollouts (rollout_logic.HealthLimits); cli.py can override.
    health_window_seconds: int = 60
    max_crashes: int = 0
    max_errors: int = 10
    max_failed_devices: int = 0
    unresponsive_seconds: int = 45
    # Read-only database login for the Grafana dashboard; created at start-up when a
    # password is set (db.Database.ensure_dashboard_reader).
    dashboard_db_user: str = "dashboard_reader"
    dashboard_db_password: str = ""
    # Broker sign-in and TLS (FR-7): the orchestrator's own account, and the CA that signed
    # the broker's certificate. Empty means anonymous / plain text (tests only).
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_ca_cert: str = ""

    def health_limits(self):
        from rollout_logic import HealthLimits
        return HealthLimits(self.health_window_seconds, self.max_crashes, self.max_errors,
                            self.max_failed_devices, self.unresponsive_seconds)


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
        registry_url=os.environ.get("REGISTRY_URL", "http://software-registry:8080").rstrip("/"),
        registry_public_url=os.environ.get(
            "REGISTRY_PUBLIC_URL", "http://host.docker.internal:8080").rstrip("/"),
        command_timeout_seconds=int(os.environ.get("COMMAND_TIMEOUT_SECONDS", "120")),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        health_window_seconds=int(os.environ.get("HEALTH_WINDOW_SECONDS", "60")),
        max_crashes=int(os.environ.get("HEALTH_MAX_CRASHES", "0")),
        max_errors=int(os.environ.get("HEALTH_MAX_ERRORS", "10")),
        max_failed_devices=int(os.environ.get("HEALTH_MAX_FAILED_DEVICES", "0")),
        unresponsive_seconds=int(os.environ.get("HEALTH_UNRESPONSIVE_SECONDS", "45")),
        dashboard_db_user=os.environ.get("GRAFANA_DB_USER", "dashboard_reader"),
        dashboard_db_password=os.environ.get("GRAFANA_DB_PASSWORD", ""),
        mqtt_username=os.environ.get("MQTT_USERNAME", ""),
        mqtt_password=os.environ.get("MQTT_PASSWORD", ""),
        mqtt_ca_cert=os.environ.get("MQTT_CA_CERT", ""),
    )
