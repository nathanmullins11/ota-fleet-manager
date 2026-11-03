# mqtt-bridge

A small broker-to-broker MQTT bridge in Python (paho-mqtt 2.x). It forwards selected
topics between a **local** broker (device/edge) and a **remote** broker (fleet/cloud),
with topic remapping, at-least-once delivery, loop prevention and buffering across outages.

```
pip install paho-mqtt pyyaml
python bridge.py config.yaml
```

## Architecture

```
  local broker                       bridge process                       remote broker
 ┌────────────┐   on_message   ┌──────────────────────────┐   publish   ┌────────────┐
 │ telemetry/#├──────────────► │ paho client "local"      │             │            │
 │ events/#   │                │   (network thread)       │             │ fleet/r1/… │
 │ cmd/#   ◄──┼──── publish ── │          │ callbacks     │ ──────────► │            │
 └────────────┘                │          ▼ (enqueue only)│             └────────────┘
                               │   ┌──────────────┐       │                   ▲
                               │   │ event queue  │◄──────┼── callbacks ──────┤
                               │   └──────┬───────┘       │   paho client "remote"
                               │          ▼               │
                               │   worker thread: route,  │
                               │   remap, dedup, buffer,  │
                               │   publish, ACK           │
                               └──────────────────────────┘
```

Each broker gets its own paho client and network thread. **Callbacks never block and
never touch shared state** — they push `(kind, side, …)` events onto one queue. A single
worker thread owns all bridge state (buffers, in-flight table, dedup cache) and makes every
`publish()` and `ack()` call.

This is deliberate. The first version used per-side locks and published straight from the
callbacks; under load the two network threads deadlocked (thread A held side B's lock
while blocked inside B's `publish()`, B's network thread held paho's internal mutex while
waiting for that lock in `on_publish`). The single-worker design removes cross-thread
locks entirely and also removes the "PUBACK arrives before I've recorded the mid" race,
since events are handled in order.

## Rules and topic mapping

```yaml
rules:
  - topic: "telemetry/#"       # filter, relative to the source-side prefix
    direction: out             # out = local→remote, in = remote→local, both
    remote_prefix: "fleet/robot-042/"
    qos: 0
```

- The source subscription is `<source prefix> + topic`; on forwarding, the source prefix is
  stripped and the destination prefix added. So `cmd/#` with `direction: in` and
  `remote_prefix: fleet/robot-042/` subscribes to `fleet/robot-042/cmd/#` on the remote and
  republishes `fleet/robot-042/cmd/ota` locally as `cmd/ota`.
- First matching rule wins. Effective QoS is `min(received QoS, rule QoS)`.
- Prefixing per device keeps one cloud namespace for the whole fleet and lets the cloud
  broker's ACLs pin each device to `fleet/<its-id>/#`.

## Delivery semantics

| Rule QoS | Guarantee |
|---|---|
| 0 | best effort |
| 1, 2 | **at-least-once end to end** |

For QoS ≥ 1 the bridge uses paho's `manual_ack`: the PUBACK to the *source* broker is only
sent after the *destination* broker has ACKed the forwarded copy. If the bridge dies
mid-flight, the source broker still considers the message undelivered and redelivers it to
the bridge's persistent session on restart. Duplicates are possible (that's what
at-least-once means); consumers that care should be idempotent (e.g. carry a message ID).
QoS 2 is accepted but gives at-least-once across the bridge, not exactly-once.

Sessions are persistent by default (`clean_start: false`, v5 session expiry 1 h), so the
brokers queue QoS ≥ 1 traffic for the bridge while it's restarting.

## Loop prevention (`direction: both`)

1. **MQTT v5 No Local** on every subscription: a broker never sends the bridge its own
   publishes back.
2. **Dedup cache** (`dedup_ttl`, default 5 s): every forwarded `(side, topic, payload)` is
   remembered briefly, and a matching inbound message is dropped as an echo. This is the
   only guard on MQTT 3.1.1 brokers, which lack No Local.

Caveat: if two different publishers send an identical payload on the same topic within the
TTL, the second may be suppressed. Shorten the TTL, or use v5 brokers, if that matters.

## Outages and buffering

- paho reconnects with exponential backoff (1 s → 60 s).
- While the destination is down, messages wait in a per-direction in-memory buffer
  (`buffer_size`, default 10 000). When full, the oldest is dropped *and ACKed at its source*
  so that session doesn't stall; drops are logged and counted.
- QoS ≥ 1 messages already handed to paho stay with paho, which retransmits them on
  reconnect.
- Upstream back-pressure comes for free: un-ACKed source messages count against the
  source broker's in-flight window, so a slow destination slows delivery to the bridge.

## Status and observability

- Retained `bridge/<bridge_id>/status` = `online` / `offline` on both brokers, with
  `offline` also set as the Last Will so a crashed bridge is visible.
- A stats line every `stats_interval` seconds: forwarded, echoes suppressed, buffered,
  in flight, dropped.

## Security

TLS with CA and per-device client certificates (`tls`, `ca_certs`, `certfile`, `keyfile`)
or username/password. For a fleet, prefer one client cert per device and broker-side ACLs
restricting each device to its own prefix.

## Testing

`test_bridge.py` runs end to end against two real brokers on 127.0.0.1:1883 / 1884 with
the bridge running (rules listed at the top of the file):

1. out direction with prefix added
2. in direction with prefix stripped
3. unmatched topics are not forwarded
4. bidirectional sync with no echo loop
5. 2 000 QoS 1 messages, all delivered
6. remote broker killed, 100 QoS 1 messages published, broker restarted: all 100 arrive
   (needs `--outage-cmd-stop/--outage-cmd-start` and persistence enabled on the remote
   broker, since the test's subscriber has to survive the restart)

All six pass against Mosquitto 2 with the bridge in both `v5` and `v311` mode.

## When not to use this

Mosquitto, EMQX and HiveMQ all have built-in bridging configured in the broker. If plain
topic forwarding is all you need, that's less to run. A standalone bridge earns its keep when
you want logic in the path: per-device remapping computed at runtime, payload validation or
transforms, rate limiting, filtering by content, or custom metrics.

## Possible next steps

- Spill the buffer to disk (SQLite) so long outages survive a bridge restart
- Prometheus metrics endpoint instead of log lines
- Payload transform hook per rule (e.g. JSON → CBOR on the uplink)
- Per-rule rate limits for telemetry
