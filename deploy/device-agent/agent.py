"""Simulated IoT device agent.

Runs the installed firmware (firmware_runner.py), reports its health in a heartbeat on a
fixed interval, and carries out update and rollback commands from the orchestrator
(installer.py), answering each with events. Topics and messages follow
deploy/mqtt/mqtt-topics-and-messages.md; the firmware interface is in firmware/README.md.

Commands are handled one at a time, in the order they arrive, on a worker thread: a
command that arrives during an update waits for it to finish. A command for the version
that is already running changes nothing and is answered with success (idempotent), so a
repeated or redelivered command is harmless.

Not implemented: TLS and broker authentication.
"""
import json
import logging
import queue
import signal
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from commands import Command, InvalidCommand, parse_command
from config import Config, load_config
from firmware_runner import FirmwareRunner
from installer import (InstallError, Installed, Store, VerificationError, download_verified,
                       load_public_key)

log = logging.getLogger("device-agent")


def heartbeat_topic(device_id: str) -> str:
    return f"ota/devices/{device_id}/heartbeat"


def event_topic(device_id: str) -> str:
    return f"ota/devices/{device_id}/events"


def command_topic(device_id: str) -> str:
    return f"ota/devices/{device_id}/commands"


def utc_now() -> str:
    """ISO 8601 UTC with a trailing Z, as the contract requires."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_heartbeat(device_id: str, version: str, status: str, error_count: int,
                    crash_count: int) -> dict:
    return {
        "schema_version": 1,
        "device_id": device_id,
        "version": version,
        "status": status,               # healthy, degraded, unhealthy or updating
        "error_count": error_count,     # errors since the previous heartbeat
        "crash_count": crash_count,     # firmware crashes since the previous heartbeat
        "timestamp": utc_now(),
    }


def build_event(device_id: str, event: str, rollout_id: int, version: str,
                reason: Optional[str] = None) -> dict:
    return {
        "schema_version": 1,
        "device_id": device_id,
        "event": event,
        "rollout_id": rollout_id,
        "version": version,
        "reason": reason,
        "timestamp": utc_now(),
    }


EventSink = Callable[[str, int, str, Optional[str]], None]   # (event, rollout_id, version, reason)


class Device:
    """The device's update logic, independent of MQTT (tests drive it directly)."""

    def __init__(self, cfg: Config, store: Store, runner: FirmwareRunner, send_event: EventSink):
        self.cfg = cfg
        self.store = store
        self.runner = runner
        self.send_event = send_event
        self.busy = threading.Event()

    @property
    def version(self) -> str:
        return self.store.current

    def boot(self) -> None:
        installed = self.store.bootable(self.cfg.factory_dir)
        self.runner.start(installed.version, installed.app_dir, installed.entrypoint)
        problem = self.runner.wait_until_started(self.cfg.start_timeout_seconds)
        if problem:
            log.warning("At start-up: %s", problem)

    def heartbeat(self) -> dict:
        health = self.runner.take_health()
        status = "updating" if self.busy.is_set() else health.status
        return build_heartbeat(self.cfg.device_id, self.version, status,
                               health.errors, health.crashes)

    def handle(self, cmd: Command) -> None:
        kind, version = cmd.command, cmd.target_version
        if version == self.version:
            log.info("Already running %s; nothing to do for %s (rollout %d)",
                     version, kind, cmd.rollout_id)
            self.send_event(f"{kind}_succeeded", cmd.rollout_id, version,
                            f"already running {version}")
            return

        self.send_event(f"{kind}_started", cmd.rollout_id, version, None)
        self.busy.set()
        try:
            installed = self._obtain(cmd)
            problem = self._switch_to(installed)
        except (VerificationError, InstallError) as exc:
            problem = str(exc)
        except Exception as exc:        # never leave a command unanswered
            log.exception("Unexpected error during %s to %s", kind, version)
            problem = f"internal error: {exc}"
        finally:
            self.busy.clear()

        if problem:
            log.warning("%s to %s failed: %s", kind.capitalize(), version, problem)
            self.send_event(f"{kind}_failed", cmd.rollout_id, version, problem)
        else:
            log.info("%s to %s succeeded", kind.capitalize(), version)
            self.send_event(f"{kind}_succeeded", cmd.rollout_id, version, None)

    def _obtain(self, cmd: Command) -> Installed:
        """A verified, unpacked copy of the target: the kept local copy if it matches the
        command's checksum (normal for a rollback), otherwise a fresh download."""
        if self.store.has_valid(cmd.target_version, cmd.sha256):
            log.info("Using the kept copy of %s", cmd.target_version)
            return self.store.installed(cmd.target_version)
        log.info("Downloading %s from %s", cmd.target_version, cmd.download_url)
        download = download_verified(cmd.manifest_url, cmd.signature_url, cmd.download_url,
                                     self.store.public_key, cmd.target_version, cmd.sha256,
                                     self.cfg.download_timeout_seconds)
        return self.store.add(cmd.target_version, download)

    def _switch_to(self, new: Installed) -> Optional[str]:
        """Stop the running firmware and start `new`. If it does not start properly, go back
        to the old version. Returns None on success, otherwise the reason."""
        old = self.store.installed(self.version)
        self.runner.stop()
        self.runner.start(new.version, new.app_dir, new.entrypoint, trial=True)
        problem = self.runner.wait_until_started(self.cfg.start_timeout_seconds)
        if problem is None:
            self.store.set_current(new.version)
            return None
        self.runner.stop()
        self.runner.start(old.version, old.app_dir, old.entrypoint)
        return f"{problem}; still running {old.version}"


# --------------------------------------------------------------------------- MQTT wiring

def build_client(cfg: Config, connected: threading.Event,
                 on_command: Callable[[bytes], None]) -> mqtt.Client:
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
        on_command(msg.payload)

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

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    try:
        public_key = load_public_key(cfg.public_key_path)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot load the release public key: {exc}") from None
    store = Store(cfg.firmware_dir, public_key)
    runner = FirmwareRunner(cfg.restart_delay_seconds)
    commands: "queue.Queue[Command]" = queue.Queue()
    client: Optional[mqtt.Client] = None

    def send_event(event: str, rollout_id: int, version: str, reason: Optional[str]) -> None:
        payload = build_event(cfg.device_id, event, rollout_id, version, reason)
        # QoS 1: paho keeps it and sends it after a reconnect if the broker is unreachable.
        client.publish(event_topic(cfg.device_id), json.dumps(payload), qos=1)
        log.info("Event sent: %s %s (rollout %d)%s", event, version, rollout_id,
                 f": {reason}" if reason else "")

    def on_command(payload: bytes) -> None:
        try:
            cmd = parse_command(payload)
        except InvalidCommand as exc:
            log.warning("Rejected command: %s (payload %r)", exc.reason, payload[:200])
            if exc.answerable:
                send_event(f"{exc.command}_failed", exc.rollout_id, exc.version,
                           f"invalid command: {exc.reason}")
            return
        log.info("Command received: %s to %s (rollout %d)%s", cmd.command, cmd.target_version,
                 cmd.rollout_id, "; queued behind the current one" if device.busy.is_set() else "")
        commands.put(cmd)

    device = Device(cfg, store, runner, send_event)
    try:
        device.boot()
    except (InstallError, VerificationError, OSError) as exc:
        raise SystemExit(f"Cannot start firmware: {exc}") from None
    log.info("Starting %s (firmware %s, heartbeat every %ss)",
             cfg.device_id, device.version, cfg.heartbeat_interval_seconds)

    def work() -> None:
        while not stop.is_set():
            try:
                cmd = commands.get(timeout=0.5)
            except queue.Empty:
                continue
            device.handle(cmd)

    connected = threading.Event()
    client = build_client(cfg, connected, on_command)
    if not connect_with_retry(client, cfg, stop):
        runner.stop()
        return
    client.loop_start()
    threading.Thread(target=work, name="commands", daemon=True).start()
    connected.wait(timeout=10)        # avoid a spurious "not connected" on the first beat

    topic = heartbeat_topic(cfg.device_id)
    log.info("Publishing heartbeats to %s", topic)
    while not stop.is_set():
        if connected.is_set():
            hb = device.heartbeat()      # only now, so skipped beats do not lose crash counts
            client.publish(topic, json.dumps(hb), qos=0)   # QoS 0, not retained
            log.debug("Heartbeat sent: version=%s status=%s errors=%d crashes=%d",
                      hb["version"], hb["status"], hb["error_count"], hb["crash_count"])
        else:
            log.warning("Not connected to broker; heartbeat skipped")
        stop.wait(cfg.heartbeat_interval_seconds)

    log.info("Shutting down")
    runner.stop()
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
