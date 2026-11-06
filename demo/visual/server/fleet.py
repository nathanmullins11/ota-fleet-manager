"""The simulated devices on this computer (Docker) and an MQTT connection for the visual demo.

Device containers use the same names and label as the terminal demo
(ota-demo-device-NNN, label ota-demo=device), so either demo's cleanup removes them.
"""
import collections
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import docker
import paho.mqtt.client as mqtt

LABEL_KEY, LABEL_VALUE = "ota-demo", "device"
NAME_RE = re.compile(r"ota-demo-(device-\d{3,})$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def container_name(device_id: str) -> str:
    return f"ota-demo-{device_id}"


class Fleet:
    """Start, stop and list the device containers."""

    def __init__(self, image: str, mqtt_host: str, mqtt_port: int):
        self.docker = docker.from_env()
        self.image = image
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port

    def containers(self) -> dict:
        """device_id -> 'running' | 'stopped' for every demo device container."""
        result = {}
        for c in self.docker.containers.list(all=True,
                                             filters={"label": f"{LABEL_KEY}={LABEL_VALUE}"}):
            m = NAME_RE.match(c.name)
            if m:
                result[m[1]] = "running" if c.status == "running" else "stopped"
        return result

    def start(self, device_id: str) -> None:
        try:
            c = self.docker.containers.get(container_name(device_id))
            c.start()
            return
        except docker.errors.NotFound:
            pass
        self.docker.containers.run(
            self.image, name=container_name(device_id), detach=True,
            labels={LABEL_KEY: LABEL_VALUE},
            extra_hosts={"host.docker.internal": "host-gateway"},
            environment={"DEVICE_ID": device_id, "MQTT_HOST": self.mqtt_host,
                         "MQTT_PORT": str(self.mqtt_port), "FIRMWARE_VERSION": "1.0",
                         "HEARTBEAT_INTERVAL_SECONDS": "5"})

    def stop(self, device_id: str) -> None:
        self.docker.containers.get(container_name(device_id)).stop(timeout=2)

    def next_device_id(self) -> str:
        used = {int(d.split("-")[1]) for d in self.containers()}
        n = 1
        while n in used:
            n += 1
        return f"device-{n:03d}"

    def logs(self, device_id: str, tail: int = 20) -> str:
        return self.docker.containers.get(container_name(device_id)).logs(
            tail=tail).decode("utf-8", "replace")

    def remove_all(self) -> None:
        for c in self.docker.containers.list(all=True,
                                             filters={"label": f"{LABEL_KEY}={LABEL_VALUE}"}):
            if NAME_RE.match(c.name):
                c.remove(force=True)


class Mqtt:
    """Listens to every device's heartbeats and commands, and publishes test messages."""

    def __init__(self, host: str, port: int):
        self.lock = threading.Lock()
        self.heartbeats = {}                               # device_id -> (monotonic, payload)
        self.commands = collections.deque(maxlen=300)      # newest last
        self.connected = False
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"ota-visual-demo-{uuid.uuid4().hex[:6]}")
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(1, 10)
        self.client.connect_async(host, port, keepalive=30)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            return
        self.connected = True
        client.subscribe("ota/devices/+/heartbeat", qos=0)
        client.subscribe("ota/devices/+/commands", qos=1)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False

    def _on_message(self, client, userdata, msg):
        parts = msg.topic.split("/")
        if len(parts) != 4:
            return
        device_id, kind = parts[2], parts[3]
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            payload = None
        with self.lock:
            if kind == "heartbeat" and isinstance(payload, dict):
                self.heartbeats[device_id] = (time.monotonic(), payload)
            elif kind == "commands" and isinstance(payload, dict):
                self.commands.append({"time": utc_now(), "device_id": device_id,
                                      "payload": payload})

    def heartbeat_ages(self) -> dict:
        now = time.monotonic()
        with self.lock:
            return {d: round(now - t, 1) for d, (t, _p) in self.heartbeats.items()}

    def recent_commands(self, limit: int = 20) -> list:
        with self.lock:
            return list(self.commands)[-limit:]

    def commands_for(self, command: str, rollout_id: int) -> dict:
        """device_id -> payload of the latest command of this kind for this rollout."""
        with self.lock:
            return {c["device_id"]: c["payload"] for c in self.commands
                    if c["payload"].get("command") == command
                    and c["payload"].get("rollout_id") == rollout_id}

    def publish(self, topic: str, payload, qos: int = 1) -> None:
        body = payload if isinstance(payload, str) else json.dumps(payload)
        info = self.client.publish(topic, body, qos=qos)
        info.wait_for_publish(timeout=5)

    def heartbeat(self, device_id: str, **fields) -> None:
        msg = {"schema_version": 1, "device_id": device_id, "version": "1.0",
               "status": "healthy", "error_count": 0, "timestamp": utc_now()}
        msg.update(fields)
        self.publish(f"ota/devices/{device_id}/heartbeat", msg)

    def event(self, device_id: str, event: str, rollout_id: int, version: str,
              reason=None) -> None:
        self.publish(f"ota/devices/{device_id}/events",
                     {"schema_version": 1, "device_id": device_id, "event": event,
                      "rollout_id": rollout_id, "version": version, "reason": reason,
                      "timestamp": utc_now()})
