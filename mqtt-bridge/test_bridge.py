#!/usr/bin/env python3
"""
End-to-end tests for bridge.py against two real brokers.

Expects brokers on 127.0.0.1:1883 (local) and 127.0.0.1:1884 (remote) and the
bridge already running with rules:
  telemetry/# out  -> fleet/r1/   qos0
  events/#    out  -> fleet/r1/   qos1
  cmd/#       in   <- fleet/r1/   qos1
  config/#    both                qos1
Pass --outage-cmd-stop / --outage-cmd-start to also test buffering across a
remote broker outage.
"""
import argparse
import subprocess
import threading
import time
import uuid

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion


class Probe:
    def __init__(self, port, subs, client_id=None):
        self.msgs = []
        self.lock = threading.Lock()
        # a fixed client_id gets a persistent session (broker queues while we're away)
        self.c = mqtt.Client(CallbackAPIVersion.VERSION2,
                             client_id=client_id or f"probe-{uuid.uuid4().hex[:6]}",
                             protocol=mqtt.MQTTv311, clean_session=client_id is None)
        self.c.on_message = self._on_msg
        ready = threading.Event()
        self.c.on_connect = lambda c, *a: (c.subscribe([(s, 1) for s in subs]), ready.set())
        self.c.connect("127.0.0.1", port)
        self.c.loop_start()
        ready.wait(5)
        time.sleep(0.3)

    def _on_msg(self, c, u, m):
        with self.lock:
            self.msgs.append((m.topic, m.payload.decode()))

    def pub(self, topic, payload, qos=1):
        self.c.publish(topic, payload, qos=qos).wait_for_publish(5)

    def wait_count(self, n, timeout=10.0, pred=lambda m: True):
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                got = [m for m in self.msgs if pred(m)]
            if len(got) >= n:
                return got
            time.sleep(0.05)
        with self.lock:
            return [m for m in self.msgs if pred(m)]

    def clear(self):
        with self.lock:
            self.msgs.clear()

    def close(self):
        self.c.disconnect()
        self.c.loop_stop()


results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outage-cmd-stop")
    ap.add_argument("--outage-cmd-start")
    args = ap.parse_args()

    local = Probe(1883, ["#"])
    remote = Probe(1884, ["#"])
    not_bridge = lambda m: not m[0].startswith("bridge/")

    # 1. out, prefix added
    local.pub("telemetry/battery", "87", qos=0)
    got = remote.wait_count(1, pred=lambda m: m[0] == "fleet/r1/telemetry/battery")
    check("out: telemetry/battery -> fleet/r1/telemetry/battery", got == [("fleet/r1/telemetry/battery", "87")], got)

    # 2. in, prefix stripped
    remote.pub("fleet/r1/cmd/ota", "v2.3.1")
    got = local.wait_count(1, pred=lambda m: m[0] == "cmd/ota")
    check("in: fleet/r1/cmd/ota -> cmd/ota", got == [("cmd/ota", "v2.3.1")], got)

    # 3. unmatched topics stay put
    local.pub("private/secret", "x")
    remote.pub("fleet/r2/cmd/ota", "y")
    time.sleep(1)
    leaked = [m for m in remote.msgs if "secret" in m[0]] + [m for m in local.msgs if m[0].endswith("r2/cmd/ota") or m == ("cmd/ota", "y")]
    check("unmatched topics are not forwarded", not leaked, leaked)

    # 4. bidirectional without loops
    local.clear(); remote.clear()
    local.pub("config/rate", "10")
    remote.pub("config/mode", "auto")
    time.sleep(2)
    l = [m for m in local.msgs if m[0].startswith("config/")]
    r = [m for m in remote.msgs if m[0].startswith("config/")]
    check("both: each side sees each config message exactly once (no echo loop)",
          sorted(l) == sorted(r) == [("config/mode", "auto"), ("config/rate", "10")], f"local={l} remote={r}")

    # 5. throughput / no deadlock, ordered
    N = 2000
    remote.clear()
    for i in range(N):
        local.c.publish("events/seq", str(i), qos=1)
    got = remote.wait_count(N, timeout=30, pred=lambda m: m[0] == "fleet/r1/events/seq")
    seq = [int(p) for _, p in got]
    check(f"load: {N} QoS1 messages forwarded", len(set(seq)) == N, f"got {len(set(seq))} unique")

    # 6. remote outage: buffer, then drain after reconnect
    if args.outage_cmd_stop:
        # register a persistent subscriber so the (persistence-enabled) remote
        # broker queues for it across the restart, whoever reconnects first
        remote.close()
        Probe(1884, ["fleet/r1/events/outage"], client_id="outage-probe").close()
        time.sleep(0.5)
        subprocess.run(args.outage_cmd_stop, shell=True)
        time.sleep(1)
        for i in range(100):
            local.pub("events/outage", str(i))
        time.sleep(1)
        subprocess.run(args.outage_cmd_start, shell=True)
        time.sleep(3)  # let the bridge reconnect and drain first
        remote = Probe(1884, ["fleet/r1/events/outage"], client_id="outage-probe")
        got = remote.wait_count(100, timeout=90)
        vals = sorted({int(p) for _, p in got})
        check("outage: 100 QoS1 messages published while remote down all arrive after reconnect",
              vals == list(range(100)), f"got {len(vals)}")

    local.close(); remote.close()
    print(f"\n{sum(results)}/{len(results)} passed")
    raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
