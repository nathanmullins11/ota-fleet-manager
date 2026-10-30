# Expected Behavior: What You Should See

What to expect after `docker compose up -d --build` (from `deploy/`) in each of three situations. Log wording may differ slightly from what is shown here, but the pattern should match.

| Scenario | Broker | Device |
|---|---|---|
| [1](#1-no-broker-no-device) | not running | not running |
| [2](#2-broker-no-device) | running | not running |
| [3](#3-broker-and-device) | running | running |

## Quick check commands

```bash
docker compose ps                                   # service states
docker compose logs orchestrator                    # orchestrator output
docker compose logs -f orchestrator                 # follow it live
docker compose exec postgres psql -U ota -d ota -c \
  "SELECT device_id, current_version, status, error_count, seconds_since_heartbeat FROM fleet_overview;"
curl localhost:8080/versions                        # software registry
```

To see every accepted heartbeat in the logs, set `LOG_LEVEL=DEBUG` in `.env` and restart the orchestrator (`docker compose up -d orchestrator`).

## 1. No broker, no device

This is the situation today, before the MQTT broker task is merged.

**`docker compose ps`**

| Service | State |
|---|---|
| `postgres` | Up (healthy) |
| `software-registry` | Up (healthy) |
| `orchestrator` | Up (no health label) |

**Orchestrator logs**

```
Starting orchestrator (heartbeat timeout 30s)
Cannot reach broker at mosquitto:1883 (<name resolution error>); retrying in 3s
Cannot reach broker at mosquitto:1883 (<name resolution error>); retrying in 3s
...
```

The warning repeats every 3 seconds. The error text depends on your system, for example `Name or service not known`. There is no `Connected to database` line, because the orchestrator does nothing else until it reaches the broker.

**Database:** seven tables exist, seeded with 4 rollout groups and one firmware version (`1.0`). `devices` and `heartbeats` are empty.

**Software registry:** works independently. `curl localhost:8080/versions` lists 1.0, 2.0, and 2.1.

**Is this a problem?** No. The orchestrator is waiting for a broker that does not exist yet, and it connects by itself once one does.

## 2. Broker, no device

**`docker compose ps`:** the same three services, plus the `mosquitto` service.

**Orchestrator logs**

```
Starting orchestrator (heartbeat timeout 30s)
Connected to broker mosquitto:1883
Connected to database ota at postgres:5432
```

Then the logs go quiet. The offline check runs every 5 seconds, but it only logs when it marks a device offline, and there are no devices.

**Database:** `devices` and `heartbeats` are still empty. Nothing is publishing, so nothing is recorded.

**If it does not connect to the broker:**

| Log message | Likely cause |
|---|---|
| `Cannot reach broker ... retrying` | The broker is not running, or the host name is not `mosquitto` |
| `Broker refused the connection` | The broker config does not allow this client (no listener on 1883, or anonymous connections not allowed) |

**Optional check without a device:** publish a test heartbeat by hand (the command is in the README, under "Orchestrator"). The device should appear in `fleet_overview`.

## 3. Broker and device

Assumes the device agent publishes valid heartbeats about every 5 seconds.

**Orchestrator logs (default `INFO` level)**

```
Connected to broker mosquitto:1883
Connected to database ota at postgres:5432
Connected to database ota at postgres:5432      <- second line, on the first heartbeat
```

The second `Connected to database` line is normal. The orchestrator uses two database connections (one for incoming messages, one for the offline check), and each connects when first needed. After that the logs are quiet, because accepted heartbeats are only logged at `DEBUG`.

With `LOG_LEVEL=DEBUG`, one line per heartbeat:

```
Heartbeat device-001 version=1.0 status=healthy errors=0
```

**Database (the real evidence)**

| Table or view | What you should see |
|---|---|
| `devices` | One row per device, created automatically on its first heartbeat. Shows version, status, error count, and `last_heartbeat`. `rollout_group` and `previous_version` stay empty for now |
| `heartbeats` | A new row about every 5 seconds per device (around 12 per minute each) |
| `fleet_overview` | `seconds_since_heartbeat` cycles between 0 and 5 for a healthy device. Run the query a few times to watch it |

**If you stop the device container**

After 30 seconds with no heartbeat:

```
Device device-001 marked offline (no heartbeat for 30s)
```

`devices.status` becomes `offline`. When the device starts again, its next heartbeat sets the status back to whatever it reports. Its older `heartbeats` rows stay.

**If the device never appears in `devices`**

Check the orchestrator logs for `Rejected heartbeat message ...`. Each warning names the reason. The most likely causes on a first connection:

| Warning contains | Problem |
|---|---|
| `timestamp must include a time zone` | Timestamp has no zone. In Python, use `datetime.now(timezone.utc).isoformat()`, not `datetime.utcnow().isoformat()` |
| `unsupported schema_version` | `schema_version` is missing or is not `1` |
| `does not match topic` | `device_id` in the message differs from the one in the topic |
| `Ignoring message on unexpected topic` | Device ID is not in the form `device-001`, or the topic is misspelled |
| `status must be one of` | Status is not `healthy`, `degraded`, `unhealthy`, or `updating` |
| `error_count must be an integer` | `error_count` was sent as text (`"0"`) or a decimal |
| `version must look like` | Version is not like `1.0` or `2.1.3` |

If there are no warnings at all, nothing is reaching the orchestrator. Check that the device and orchestrator use the same broker and that the topic is spelled exactly `ota/devices/<id>/heartbeat`.

**Growth to be aware of:** at 50 devices and a 5-second interval, `heartbeats` gains about 600 rows a minute. That is fine for the project, and cleanup is listed as a known limitation in `database-schema.md`.