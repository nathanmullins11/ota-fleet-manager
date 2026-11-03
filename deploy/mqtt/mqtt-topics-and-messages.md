# MQTT Topics and Messages

The contract between the **orchestrator** (control plane) and the **device agent** (fleet). Both sides must follow it exactly, because the broker does not check anything: a misspelled topic or a missing field fails silently.

- **Status:** Draft for review. Items marked **To confirm** need agreement before either side builds against them.
- **Related:** `database-schema.md` (where these messages end up in the registry)

## 1. Concepts in brief

- A **message** is one small JSON document, such as "device-001 is running 1.0 and is healthy."
- A **topic** is the text label a message is published to, such as `ota/devices/device-001/heartbeat`.
- Topics are not created or declared anywhere. They exist when someone publishes or subscribes to them, so the names below are a convention that both programs must follow.
- The broker (Mosquitto) passes each message to everyone subscribed to that topic. Publishers and subscribers never talk to each other directly.

## 2. Topics

`{id}` is the device ID, for example `device-001`. Each device has its own copy of each topic.

| Topic | Published by | Subscribed to by | Purpose |
|---|---|---|---|
| `ota/devices/{id}/heartbeat` | Device | Orchestrator | Regular snapshot of version, status, and errors |
| `ota/devices/{id}/events` | Device | Orchestrator | One-off happenings: update started, succeeded, failed, rollback done |
| `ota/devices/{id}/commands` | Orchestrator | The one device `{id}` | Instructions: update or roll back |

**Subscriptions:**

| Who | Subscribes to | Why |
|---|---|---|
| Orchestrator | `ota/devices/+/heartbeat` | Hears every device's heartbeat with one subscription (`+` matches any single device ID) |
| Orchestrator | `ota/devices/+/events` | Hears every device's events |
| Device `device-001` | `ota/devices/device-001/commands` | Hears only its own commands |

**Naming rules:**
- All lowercase, with hyphens inside names. No spaces, no leading or trailing slash.
- Device IDs match `device-NNN` (for example `device-001`). They must never contain `/`, `+`, or `#`, which MQTT treats specially.
- The device ID in the **topic is authoritative**. The payload repeats it as a sanity check, and the orchestrator logs and ignores a message where the two disagree.

## 3. Common rules for every message

- **Format:** JSON, UTF-8, one object per message.
- **`schema_version`:** integer, currently `1`. Increase it if a message format changes in a breaking way, so a running device and a newer orchestrator can tell.
- **Timestamps:** ISO 8601 in UTC with a trailing `Z`, for example `2026-10-29T14:03:07Z`.
- **Version strings:** digits and dots, such as `1.0` or `2.1`. They must match the file names in the software registry.
- **Unknown fields are ignored** by the receiver, so fields can be added later without breaking anyone.
- **Bad messages never crash the receiver.** Invalid JSON or a missing required field is logged and dropped.

## 4. Messages

### 4.1 Heartbeat (device to orchestrator)

**Topic:** `ota/devices/{id}/heartbeat`

Sent on a fixed interval (suggested: every 5 seconds, configurable on the agent).

```json
{
  "schema_version": 1,
  "device_id": "device-001",
  "version": "1.0",
  "status": "healthy",
  "error_count": 0,
  "timestamp": "2026-10-29T14:03:07Z"
}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `schema_version` | integer | yes | Format version, currently `1` |
| `device_id` | string | yes | Must match the `{id}` in the topic |
| `version` | string | yes | Firmware version the device is running now |
| `status` | string | yes | `healthy`, `degraded`, `unhealthy`, or `updating` |
| `error_count` | integer | yes | Errors since the previous heartbeat (not a running total) |
| `timestamp` | string | yes | Device clock when the message was created |

**Status values the device may send:** `healthy`, `degraded`, `unhealthy`, `updating`. The values `offline` and `unknown` in the database are set by the orchestrator (for example when heartbeats stop). A device never sends them.

**Heartbeat to database mapping:**

| Message field | Database column |
|---|---|
| `device_id` | `devices.device_id`, `heartbeats.device_id` |
| `version` | `devices.current_version`, `heartbeats.reported_version` |
| `status` | `devices.status`, `heartbeats.status` |
| `error_count` | `devices.error_count`, `heartbeats.error_count` |
| `timestamp` | `heartbeats.reported_at` |
| (time of arrival) | `devices.last_heartbeat`, `heartbeats.received_at` |

### 4.2 Event (device to orchestrator)

**Topic:** `ota/devices/{id}/events`

Sent once when something notable happens. Unlike heartbeats, events are irregular and carry a reason when something went wrong.

```json
{
  "schema_version": 1,
  "device_id": "device-001",
  "event": "update_failed",
  "rollout_id": 7,
  "version": "2.1",
  "reason": "checksum mismatch",
  "timestamp": "2026-10-29T14:05:41Z"
}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `schema_version` | integer | yes | Currently `1` |
| `device_id` | string | yes | Must match the topic |
| `event` | string | yes | One of the event names below |
| `rollout_id` | integer | yes | The rollout this relates to, copied from the command |
| `version` | string | yes | The version the event is about (the one being installed or restored) |
| `reason` | string or null | no | Short explanation, mainly for failures |
| `timestamp` | string | yes | Device clock |

**Event names and their effect on `rollout_devices.state`:**

| Event | Meaning | Resulting state |
|---|---|---|
| `update_started` | Device began downloading or installing | `commanded` |
| `update_succeeded` | New version installed and running | `updated` |
| `update_failed` | Download, checksum, or install failed | `failed` |
| `rollback_started` | Device began reverting | (no change) |
| `rollback_succeeded` | Previous version restored | `rolled_back` |
| `rollback_failed` | Revert did not work | `failed` |

### 4.3 Command: update (orchestrator to device)

**Topic:** `ota/devices/{id}/commands`

```json
{
  "schema_version": 1,
  "command": "update",
  "rollout_id": 7,
  "target_version": "2.0",
  "download_url": "http://203.0.113.10:8080/packages/2.0",
  "sha256": "9f2b4c...full 64-character hash...e1",
  "issued_at": "2026-10-29T14:05:30Z"
}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `schema_version` | integer | yes | Currently `1` |
| `command` | string | yes | `update` |
| `rollout_id` | integer | yes | Identifies the rollout. The device copies it into its events |
| `target_version` | string | yes | Version to install |
| `download_url` | string | yes | **Absolute** URL of the package in the software registry |
| `sha256` | string | yes | Expected checksum, taken from the orchestrator's own records |
| `issued_at` | string | yes | When the orchestrator sent it |

The registry's `/versions` endpoint returns relative URLs (`/packages/2.0`). The orchestrator combines them with the registry's address (a configuration setting) to produce the absolute URL the device needs.

**The agent verifies the download against `sha256` from this command**, not against the header the file server sends. That way the checksum and the file travel by different paths.

### 4.4 Command: rollback (orchestrator to device)

**Topic:** `ota/devices/{id}/commands`

```json
{
  "schema_version": 1,
  "command": "rollback",
  "rollout_id": 7,
  "target_version": "1.0",
  "download_url": "http://203.0.113.10:8080/packages/1.0",
  "sha256": "c41a7d...full 64-character hash...0b",
  "issued_at": "2026-10-29T14:05:52Z"
}
```

Fields are the same as the update command. `target_version` here is the version to **go back to**. The agent normally reinstalls the copy of the previous version it kept locally and verifies it against `sha256`. `download_url` is a fallback if the local copy is missing or fails verification.

**Agent behavior rules:**
- **Idempotent:** if `target_version` is already the running version, do nothing except report the current state. This makes repeated or replayed commands harmless.
- **One at a time:** ignore a new command while an update or rollback is still in progress, or queue it. (**To confirm** with David which.)
- **Always report:** send an event for every command received, success or failure.

## 5. Delivery settings

| Message | QoS | Retained | Reason |
|---|---|---|---|
| Heartbeat | 0 | no | Frequent and replaceable. Missing one is acceptable, and missed heartbeats are themselves a health signal |
| Event | 1 | no | Must not be lost, such as "update failed" |
| Command | 1 | no | A missed update or rollback should not be silent |

**QoS in plain terms:** 0 means "send once, no confirmation." 1 means "keep retrying until the receiver confirms, so it arrives at least once" (duplicates are possible, which is why the agent is idempotent).

**Retained is off** because a retained command would be replayed to a device every time it reconnects, including long after a rollout is finished.

**Client IDs:** each device uses its device ID as its MQTT client ID (for example `device-001`), and the orchestrator uses `orchestrator`. Two clients with the same ID disconnect each other, so IDs must be unique.

## 6. Open questions for David (To confirm)

1. **Heartbeat fields.** Are version, status, error count, and timestamp enough? Anything else the agent can report cheaply (uptime, CPU)? If so, a column is added to `heartbeats` before data is collected.
2. **Heartbeat interval.** Is every 5 seconds acceptable? A shorter interval speeds up failure detection but increases message volume at 50 or more devices.
3. **Error count meaning.** Errors since the last heartbeat (as written here) or a running total? The orchestrator's threshold logic depends on the answer.
4. **Commands while busy.** Ignore or queue a command that arrives mid-update?
5. **Offline catch-up.** If a device is disconnected when a command is sent, should it get the command on reconnect? This needs a persistent MQTT session. It is not required for Iteration 1 and can wait.
6. **Last will message.** Should the broker announce a device that drops off without a goodbye? Missed heartbeats already cover this, so it is optional.

## 7. Trying it by hand

With Mosquitto running locally and the `mosquitto-clients` tools installed, this lets you check the contract before any real code exists.

In one terminal, listen like the orchestrator:

```bash
mosquitto_sub -h localhost -t "ota/devices/+/heartbeat" -v
```

In another, publish like a device:

```bash
mosquitto_pub -h localhost -q 0 -t "ota/devices/device-001/heartbeat" -m '{"schema_version":1,"device_id":"device-001","version":"1.0","status":"healthy","error_count":0,"timestamp":"2026-10-29T14:03:07Z"}'
```

The first terminal should print the topic and the message. This is also the easiest way to test the orchestrator skeleton before the device agent exists.

## 8. Change log

| Date | Change | Agreed with |
|---|---|---|
| Iteration 1 | Initial draft | (pending review) |