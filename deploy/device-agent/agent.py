"""Simulated IoT device agent (Iteration 1).

Connects to the MQTT broker and publishes a heartbeat (version, status, error count)
on a fixed interval, following mqtt-topics-and-messages.md. It also listens on
its own command topic, but commands are only logged for now.

Not yet implemented (later iterations): installing firmware, signature verification,
rollback, event messages, failure modes, TLS and authentication.
"""
import json
import logging
import signal
import threading
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from config import Config, load_config

log = logging.getLogger("device-agent")


def heartbeat_topic(device_id: str) -> str:
    return f"ota/devices/{device_id}/heartbeat"


def command_topic(device_id: str) -> str:
    return f"ota/devices/{device_id}/commands"


def utc_now() -> str:
    """ISO 8601 UTC with a trailing Z, as the contract requires."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_heartbeat(cfg: Config) -> dict:
    return {
        "schema_version": 1,
        "device_id": cfg.device_id,
        "version": cfg.firmware_version,
        "status": "healthy",          # devices may send healthy/degraded/unhealthy/updating
        "error_count": 0,             # errors since the previous heartbeat
        "timestamp": utc_now(),
    }


def build_client(cfg: Config, connected: threading.Event) -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg.device_id)
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("Broker refused the connection: %s", reason_code)
            return
        log.info("Connected to broker %s:%s as %s", cfg.mqtt_host, cfg.mqtt_port, cfg.device_id)
        client.subscribe(command_topic(cfg.device_id), qos=1)   # restored after any reconnect
        connected.set()

    def on_disconnect(client, userdata, flags, reason_code, properties):
        connected.clear()
        if reason_code.is_failure:
            log.warning("Lost connection to broker (%s); paho will retry", reason_code)
        else:
            log.info("Disconnected from broker")

    def on_message(client, userdata, msg):
        # Commands (update, rollback) are implemented in Iteration 2.
        log.info("Command received on %s (ignored in Iteration 1): %s",
                 msg.topic, msg.payload[:200])

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    return client


def connect_with_retry(client: mqtt.Client, cfg: Config, stop: threading.Event) -> bool:
    while not stop.is_set():
        try:
            client.connect(cfg.mqtt_host, cfg.mqtt_port, keepalive=30)
            return True
        except OSError as exc:
            log.warning("Cannot reach broker at %s:%s (%s); retrying in 3s",
                        cfg.mqtt_host, cfg.mqtt_port, exc)
            stop.wait(3)
    return False


def main() -> None:
    cfg = load_config()
    logging.basicConfig(level=cfg.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("Starting %s (firmware %s, heartbeat every %ss)",
             cfg.device_id, cfg.firmware_version, cfg.heartbeat_interval_seconds)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    connected = threading.Event()
    client = build_client(cfg, connected)
    if not connect_with_retry(client, cfg, stop):
        return
    client.loop_start()
    connected.wait(timeout=10)        # avoid a spurious "not connected" on the first beat

    topic = heartbeat_topic(cfg.device_id)
    log.info("Publishing heartbeats to %s", topic)
    while not stop.is_set():
        if connected.is_set():
            hb = build_heartbeat(cfg)
            client.publish(topic, json.dumps(hb), qos=0)   # QoS 0, not retained
            log.debug("Heartbeat sent: version=%s status=%s errors=%d",
                      hb["version"], hb["status"], hb["error_count"])
        else:
            log.warning("Not connected to broker; heartbeat skipped")
        stop.wait(cfg.heartbeat_interval_seconds)

    log.info("Shutting down")
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()