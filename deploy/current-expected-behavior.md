# Expected Behavior: What You Should See

What to expect after `docker compose up -d --build` (from `deploy/`) with and without a device. Both scenarios are based on an end-to-end test of the same components outside Docker, so details may differ slightly, but the pattern should match.

| Scenario | Device |
|---|---|
| [1](#1-no-device) | not running |
| [2](#2-with-a-device) | running |

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

## 1. No device

**`docker compose ps`**

| Service | State |
|---|---|
| `mosquitto` | Up (healthy) |
| `postgres` | Up (healthy) |
| `software-registry` | Up (healthy) |
| `orchestrator` | Up (no health label) |

The orchestrator starts only after `mosquitto` and `postgres` are healthy.

**Orchestrator logs**

```
Starting orchestrator (heartbeat timeout 30s)
Connected to broker mosquitto:1883
Connected to database ota at postgres:5432
```

Then the logs go quiet. The offline check runs every 5 seconds, but it only logs when it marks a device offline, and there are no devices.

**Database:** seven tables exist, seeded with 4 rollout groups and one firmware version (`1.0`). `devices` and `heartbeats` are empty, because nothing is publishing yet.

**Software registry:** `curl localhost:8080/versions` lists 1.0, 2.0, and 2.1.

**If it does not connect to the broker:**

| Log message | Likely cause |
|---|---|
| `Cannot reach broker ... retrying` | The broker is not running, or the host name is not `mosquitto` |
| `Broker refused the connection` | The broker config does not allow this client (no listener on 1883, or anonymous connections not allowed) |

**Optional check without a device:** publish a test heartbeat by hand (the command is in the README, under "Orchestrator"). The device should appear in `fleet_overview`.

## 2. With a device

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
| `timestamp must be in UTC` | Timestamp has a non-UTC offset such as `+05:00`. Send UTC with a trailing `Z` (`+00:00` is also accepted) |
| `unsupported schema_version` | `schema_version` is missing or is not the integer `1` (`1.0` and `true` are rejected) |
| `does not match topic` | `device_id` in the message differs from the one in the topic |
| `Ignoring message on unexpected topic` | Device ID is not in the form `device-001`, or the topic is misspelled |
| `status must be one of` | Status is not `healthy`, `degraded`, `unhealthy`, or `updating` |
| `error_count must be an integer` | `error_count` was sent as text (`"0"`), a decimal, or is outside 0 to 2147483647 (the database column's range) |
| `crash_count must be an integer` | Same as above for `crash_count`, or it was sent as `null` (leave the field out instead) |
| `version must look like` | Version is not like `1.0` or `2.1.3` |

If there are no warnings at all, nothing is reaching the orchestrator. Check that the device and orchestrator use the same broker and that the topic is spelled exactly `ota/devices/<id>/heartbeat`.

**Growth to be aware of:** at 50 devices and a 5-second interval, `heartbeats` gains about 600 rows a minute. That is fine for the project, and cleanup is listed as a known limitation in `database-schema.md`.
