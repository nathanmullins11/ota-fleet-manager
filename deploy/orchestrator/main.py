"""Orchestrator skeleton (Iteration 1).

Listens for device heartbeats and events over MQTT and records device state in the
registry. Also marks devices offline when their heartbeats stop.

Not yet implemented (later iterations): publishing update/rollback commands,
staged rollout, health thresholds, automatic rollback.
"""
import logging
import signal
import threading
import time

import paho.mqtt.client as mqtt

from config import load_config
from db import Database
from messages import InvalidMessage, parse_event, parse_heartbeat
from topics import EVENTS_SUBSCRIPTION, HEARTBEAT_SUBSCRIPTION, parse_device_topic

log = logging.getLogger("orchestrator")


def build_client(cfg, mqtt_db: Database) -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg.mqtt_client_id)
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("Broker refused the connection: %s", reason_code)
            return
        log.info("Connected to broker %s:%s", cfg.mqtt_host, cfg.mqtt_port)
        # Subscribe here so subscriptions are restored after any reconnect.
        client.subscribe(HEARTBEAT_SUBSCRIPTION, qos=0)
        client.subscribe(EVENTS_SUBSCRIPTION, qos=1)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("Lost connection to broker (%s); paho will retry", reason_code)
        else:
            log.info("Disconnected from broker")

    def on_message(client, userdata, msg):
        # Never let one bad message stop the listener.
        try:
            handle_message(msg, mqtt_db)
        except Exception:
            log.exception("Unexpected error handling message on %s", msg.topic)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    return client


def handle_message(msg, db: Database) -> None:
    parsed = parse_device_topic(msg.topic)
    if parsed is None:
        log.warning("Ignoring message on unexpected topic %s", msg.topic)
        return
    device_id, kind = parsed
    try:
        if kind == "heartbeat":
            hb = parse_heartbeat(device_id, msg.payload)
            db.record_heartbeat(hb)
            log.debug("Heartbeat %s version=%s status=%s errors=%d",
                      hb.device_id, hb.version, hb.status, hb.error_count)
        elif kind == "events":
            ev = parse_event(device_id, msg.payload)
            # Recording events in rollout_devices arrives with rollouts (Iteration 2).
            log.info("Event from %s: %s rollout=%d version=%s reason=%s",
                     ev.device_id, ev.event, ev.rollout_id, ev.version, ev.reason)
        else:
            log.warning("Ignoring unknown topic kind %r from %s", kind, device_id)
    except InvalidMessage as exc:
        log.warning("Rejected %s message from %s: %s", kind, device_id, exc)


def connect_with_retry(client, cfg, stop: threading.Event) -> bool:
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
    log.info("Starting orchestrator (heartbeat timeout %ss)", cfg.heartbeat_timeout_seconds)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    mqtt_db = Database(cfg)    # used only by paho's callback thread
    main_db = Database(cfg)    # used only by this thread

    client = build_client(cfg, mqtt_db)
    if not connect_with_retry(client, cfg, stop):
        return
    client.loop_start()

    next_check = time.monotonic()
    while not stop.is_set():
        if time.monotonic() >= next_check:
            try:
                for device_id in main_db.mark_stale_offline(cfg.heartbeat_timeout_seconds):
                    log.warning("Device %s marked offline (no heartbeat for %ss)",
                                device_id, cfg.heartbeat_timeout_seconds)
            except Exception:
                log.exception("Offline check failed")
            next_check = time.monotonic() + cfg.offline_check_interval_seconds
        stop.wait(1)

    log.info("Shutting down")
    client.loop_stop()
    client.disconnect()
    mqtt_db.close()
    main_db.close()


if __name__ == "__main__":
    main()
