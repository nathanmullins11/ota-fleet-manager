#!/usr/bin/env python3
"""
mqtt-bridge: forward selected topics between two MQTT brokers.

    python bridge.py config.yaml

Design notes are in README.md. In short:
  * one paho client per broker ("local" and "remote"), each with its own network thread
  * paho callbacks never block: they only push events onto a queue
  * one worker thread owns all bridge state and does every publish and ACK,
    so there are no cross-thread locks to deadlock on
  * at-least-once for QoS>=1: the source message is ACKed only after the
    destination broker has ACKed the forwarded copy (paho manual_ack)
  * loop prevention: MQTT v5 No Local where available, plus a short-lived
    dedup cache that also covers MQTT 3.1.1 brokers
  * bounded in-memory buffer per direction while the destination is down
"""
from __future__ import annotations

import hashlib
import logging
import queue
import signal
import ssl
import sys
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

import paho.mqtt.client as mqtt
import yaml
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.subscribeoptions import SubscribeOptions

log = logging.getLogger("mqtt-bridge")


# --------------------------------------------------------------------------- config

@dataclass
class BrokerCfg:
    name: str
    host: str
    port: int = 1883
    client_id: str = ""
    username: str | None = None
    password: str | None = None
    tls: bool = False
    ca_certs: str | None = None
    certfile: str | None = None
    keyfile: str | None = None
    protocol: str = "v5"          # "v5" or "v311"
    keepalive: int = 30
    clean_start: bool = False     # False = persistent session, survives bridge restarts
    session_expiry: int = 3600    # v5 only: seconds the broker keeps our session while we're gone


@dataclass
class Rule:
    topic: str                    # filter, relative to the source prefix, e.g. "sensors/#"
    direction: str = "out"        # out: local->remote, in: remote->local, both
    local_prefix: str = ""        # stripped/added on the local side
    remote_prefix: str = ""       # stripped/added on the remote side
    qos: int = 1                  # max QoS used on both legs

    def __post_init__(self):
        if self.direction not in ("out", "in", "both"):
            raise ValueError(f"bad direction {self.direction!r}")
        if self.qos not in (0, 1, 2):
            raise ValueError(f"bad qos {self.qos!r}")


@dataclass
class Config:
    bridge_id: str
    local: BrokerCfg
    remote: BrokerCfg
    rules: list[Rule]
    buffer_size: int = 10_000     # max messages held per direction while dest is down
    dedup_ttl: float = 5.0        # seconds a forwarded message is remembered for loop suppression
    stats_interval: float = 60.0

    @staticmethod
    def load(path: str) -> "Config":
        with open(path) as f:
            raw = yaml.safe_load(f)
        bid = raw.get("bridge_id", "mqtt-bridge")
        local = BrokerCfg(name="local", **raw["local"])
        remote = BrokerCfg(name="remote", **raw["remote"])
        for b in (local, remote):
            b.client_id = b.client_id or f"{bid}-{b.name}"
        rules = [Rule(**r) for r in raw["rules"]]
        return Config(bid, local, remote, rules,
                      raw.get("buffer_size", 10_000), raw.get("dedup_ttl", 5.0),
                      raw.get("stats_interval", 60.0))


# --------------------------------------------------------------------------- topic mapping

def topic_matches(filt: str, topic: str) -> bool:
    """MQTT wildcard match (+ and #)."""
    f, t = filt.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(f) == len(t)


def remap(topic: str, from_prefix: str, to_prefix: str) -> str:
    if from_prefix and topic.startswith(from_prefix):
        topic = topic[len(from_prefix):]
    return to_prefix + topic


# --------------------------------------------------------------------------- dedup cache

class Dedup:
    """Remembers (side, topic, payload) we just published so the echo coming
    back on that side's subscription isn't forwarded again. Belt-and-braces
    with MQTT v5 No Local; the only loop guard on 3.1.1 brokers.
    Worker-thread only, so no locking."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._seen: OrderedDict[bytes, float] = OrderedDict()

    @staticmethod
    def _key(side: str, topic: str, payload: bytes) -> bytes:
        return hashlib.blake2b(side.encode() + b"\0" + topic.encode() + b"\0" + payload,
                               digest_size=16).digest()

    def mark(self, side: str, topic: str, payload: bytes):
        self._expire()
        k = self._key(side, topic, payload)
        self._seen.pop(k, None)
        self._seen[k] = time.monotonic() + self.ttl

    def consume(self, side: str, topic: str, payload: bytes) -> bool:
        """True if this message is our own echo (and forget it)."""
        self._expire()
        return self._seen.pop(self._key(side, topic, payload), None) is not None

    def _expire(self):
        now = time.monotonic()
        while self._seen:
            k, exp = next(iter(self._seen.items()))
            if exp > now:
                break
            self._seen.popitem(last=False)


# --------------------------------------------------------------------------- one side of the bridge

@dataclass
class Pending:
    """A message waiting to be (or being) published to the destination."""
    topic: str
    payload: bytes
    qos: int
    retain: bool
    src: "Side"
    src_mid: int | None          # mid to ACK on the source once dest confirms (None for QoS 0)
    src_qos: int


class Side:
    """One broker connection. Network callbacks only enqueue events;
    everything else runs on the bridge's worker thread."""

    def __init__(self, cfg: BrokerCfg, bridge: "Bridge"):
        self.cfg = cfg
        self.bridge = bridge
        self.name = cfg.name
        self.peer: Side | None = None
        self.v5 = cfg.protocol == "v5"
        # worker-thread state
        self.connected = False
        self.buffer: deque[Pending] = deque()
        self.inflight: dict[int, Pending] = {}   # our publish mid -> pending
        self.dropped = 0

        self.client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=cfg.client_id,
            protocol=mqtt.MQTTv5 if self.v5 else mqtt.MQTTv311,
            clean_session=None if self.v5 else cfg.clean_start,
            manual_ack=True,
        )
        if cfg.username:
            self.client.username_pw_set(cfg.username, cfg.password)
        if cfg.tls:
            self.client.tls_set(ca_certs=cfg.ca_certs, certfile=cfg.certfile,
                                keyfile=cfg.keyfile, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client.max_inflight_messages_set(100)
        self.client.max_queued_messages_set(0)      # unbounded inside paho; we bound our own buffer

        self.status_topic = f"bridge/{bridge.cfg.bridge_id}/status"
        self.client.will_set(self.status_topic, b"offline", qos=1, retain=True)

        q = bridge.events
        self.client.on_connect = lambda c, u, flags, rc, props: q.put(("connect", self, flags, rc))
        self.client.on_disconnect = lambda c, u, flags, rc, props: q.put(("disconnect", self, rc))
        self.client.on_message = lambda c, u, msg: q.put(("message", self, msg))
        self.client.on_publish = lambda c, u, mid, rc, props: q.put(("puback", self, mid, rc))

    # -- lifecycle (main thread)
    def start(self):
        props = None
        if self.v5:
            props = Properties(PacketTypes.CONNECT)
            props.SessionExpiryInterval = 0 if self.cfg.clean_start else self.cfg.session_expiry
        self.client.connect_async(
            self.cfg.host, self.cfg.port, self.cfg.keepalive,
            clean_start=self.cfg.clean_start if self.v5 else mqtt.MQTT_CLEAN_START_FIRST_ONLY,
            properties=props)
        self.client.loop_start()

    def stop(self):
        if self.client.is_connected():
            self.client.publish(self.status_topic, b"offline", qos=1, retain=True).wait_for_publish(2)
        self.client.disconnect()
        self.client.loop_stop()

    # -- event handlers (worker thread)
    def on_connect(self, flags, rc):
        if rc.is_failure:
            log.error("[%s] connect refused: %s", self.name, rc)
            return
        log.info("[%s] connected to %s:%s (session present=%s)",
                 self.name, self.cfg.host, self.cfg.port, flags.session_present)
        self.client.publish(self.status_topic, b"online", qos=1, retain=True)
        subs = self.bridge.subscriptions_for(self)
        if subs:
            if self.v5:
                self.client.subscribe([(t, SubscribeOptions(qos=q, noLocal=True, retainAsPublished=True))
                                       for t, q in subs])
            else:
                self.client.subscribe(subs)
            log.info("[%s] subscribed: %s", self.name, ", ".join(t for t, _ in subs))
        self.connected = True
        self.flush()

    def on_disconnect(self, rc):
        self.connected = False
        # In-flight QoS>=1 publishes stay with paho, which retransmits them on
        # reconnect; their PUBACKs will still arrive and release the source.
        log.warning("[%s] disconnected (%s); reconnecting. %d in flight, %d buffered",
                    self.name, rc, len(self.inflight), len(self.buffer))

    def on_puback(self, mid, rc):
        p = self.inflight.pop(mid, None)
        if p is None:
            return
        if rc.is_failure:
            log.error("[%s] broker rejected %s: %s", self.name, p.topic, rc)
        if p.src_mid is not None:
            p.src.ack(p.src_mid, p.src_qos)

    # -- outbound (worker thread)
    def ack(self, mid: int, qos: int):
        if qos > 0:
            self.client.ack(mid, qos)

    def enqueue(self, p: Pending):
        if len(self.buffer) >= self.bridge.cfg.buffer_size:
            old = self.buffer.popleft()
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 1000 == 0:
                log.warning("[%s] buffer full, dropping oldest (%s); %d dropped total",
                            self.name, old.topic, self.dropped)
            # release it at the source so that session doesn't stall
            if old.src_mid is not None:
                old.src.ack(old.src_mid, old.src_qos)
        self.buffer.append(p)
        if self.connected:
            self.flush()

    def flush(self):
        while self.buffer and self.connected:
            p = self.buffer[0]
            self.bridge.dedup.mark(self.name, p.topic, p.payload)
            info = self.client.publish(p.topic, p.payload, qos=p.qos, retain=p.retain)
            if info.rc != mqtt.MQTT_ERR_SUCCESS and p.qos == 0:
                break                      # QoS0 not taken by paho: keep it, retry on reconnect
            self.buffer.popleft()
            if p.qos == 0:
                if p.src_mid is not None:
                    p.src.ack(p.src_mid, p.src_qos)
            else:
                # QoS>=1: paho owns retransmission from here, even if rc==NO_CONN
                self.inflight[info.mid] = p


# --------------------------------------------------------------------------- bridge

class Bridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.events: queue.Queue = queue.Queue()
        self.dedup = Dedup(cfg.dedup_ttl)
        self.local = Side(cfg.local, self)
        self.remote = Side(cfg.remote, self)
        self.local.peer, self.remote.peer = self.remote, self.local
        self.forwarded = 0
        self.echoes = 0

    def _rules_from(self, side: Side):
        """Yield (rule, src_filter, src_prefix, dst_prefix) for messages leaving `side`."""
        for r in self.cfg.rules:
            if side is self.local and r.direction in ("out", "both"):
                yield r, r.local_prefix + r.topic, r.local_prefix, r.remote_prefix
            if side is self.remote and r.direction in ("in", "both"):
                yield r, r.remote_prefix + r.topic, r.remote_prefix, r.local_prefix

    def subscriptions_for(self, side: Side) -> list[tuple[str, int]]:
        subs: dict[str, int] = {}
        for r, filt, _, _ in self._rules_from(side):
            subs[filt] = max(subs.get(filt, 0), r.qos)
        return list(subs.items())

    def route(self, src: Side, msg: mqtt.MQTTMessage):
        payload = bytes(msg.payload)
        if self.dedup.consume(src.name, msg.topic, payload):
            self.echoes += 1
            src.ack(msg.mid, msg.qos)
            return
        for r, filt, src_prefix, dst_prefix in self._rules_from(src):
            if topic_matches(filt, msg.topic):
                dst_topic = remap(msg.topic, src_prefix, dst_prefix)
                log.debug("[%s->%s] %s -> %s", src.name, src.peer.name, msg.topic, dst_topic)
                src.peer.enqueue(Pending(dst_topic, payload, min(msg.qos, r.qos), msg.retain,
                                         src, msg.mid if msg.qos > 0 else None, msg.qos))
                self.forwarded += 1
                return   # first matching rule wins
        src.ack(msg.mid, msg.qos)  # subscribed, but no rule matched (overlapping filters)

    def _handle(self, ev):
        kind, side = ev[0], ev[1]
        if kind == "message":
            try:
                self.route(side, ev[2])
            except Exception:
                log.exception("[%s] error routing %s", side.name, ev[2].topic)
                side.ack(ev[2].mid, ev[2].qos)   # don't wedge the session on a poison message
        elif kind == "puback":
            side.on_puback(ev[2], ev[3])
        elif kind == "connect":
            side.on_connect(ev[2], ev[3])
        elif kind == "disconnect":
            side.on_disconnect(ev[2])

    def run(self):
        stop = threading.Event()
        for s in (signal.SIGINT, signal.SIGTERM):
            signal.signal(s, lambda *_: stop.set())
        self.local.start()
        self.remote.start()
        next_stats = time.monotonic() + self.cfg.stats_interval
        while not stop.is_set():
            try:
                self._handle(self.events.get(timeout=0.5))
            except queue.Empty:
                pass
            if time.monotonic() >= next_stats:
                next_stats += self.cfg.stats_interval
                log.info("forwarded=%d echoes_suppressed=%d buffered local=%d remote=%d "
                         "inflight local=%d remote=%d dropped=%d",
                         self.forwarded, self.echoes, len(self.local.buffer), len(self.remote.buffer),
                         len(self.local.inflight), len(self.remote.inflight),
                         self.local.dropped + self.remote.dropped)
        log.info("shutting down")
        self.local.stop()
        self.remote.stop()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    if len(sys.argv) != 2:
        sys.exit("usage: bridge.py config.yaml")
    Bridge(Config.load(sys.argv[1])).run()


if __name__ == "__main__":
    main()
