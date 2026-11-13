# Firmware

The firmware that devices install over the air. Each folder is one version:

| Version | Behavior | State schema | Purpose |
|---|---|---|---|
| `1.0` | Always healthy | 1 | Baseline every device starts on |
| `2.0` | Always healthy, also reports `uptime_seconds` | 2 (converts 1 → 2) | The good update |
| `2.1` | Healthy for 10 s, then 5 errors per tick for 5 s, then crashes (exit 1) | 2 (converts 1 → 2) | The bad update that should trigger rollback |

2.1 fails the same way on every run so that experiments are repeatable. The agent restarts it after each crash, so it keeps cycling until the device is rolled back.

## Interface between the device agent and the firmware

This is the contract the device agent codes against.

**Starting.** The agent unpacks a package into its own folder and runs `python main.py` (the manifest's `entrypoint`) with that folder as the working directory, as a child process. Firmware uses only the Python standard library.

**Status output.** Once per tick (1 second by default) the firmware prints one JSON object per line to **stdout**:

```json
{"version": "2.0", "status": "healthy", "errors": 0, "uptime_seconds": 12, "state_schema": 2, "boot_count": 3}
```

| Field | Type | Meaning |
|---|---|---|
| `version` | string | The firmware's own version |
| `status` | string | `healthy`, `degraded` or `unhealthy` |
| `errors` | integer | Errors during this tick (not a running total) |
| `state_schema`, `boot_count` | integer | Informational: the state schema in use, and how many times this device's state has been started (see [State](#state-and-rollback)) |

Other fields may appear and should be ignored. For the heartbeat, the agent reports the worst `status` and the sum of `errors` since the previous heartbeat. **stderr** is for human-readable log lines only (the agent copies them into its own log).

**State folder.** The agent passes `FIRMWARE_STATE_DIR`, a folder that survives updates and restarts, for the firmware's persistent state. See [State and rollback](#state-and-rollback).

**Stopping.** On SIGTERM (or SIGINT) the firmware stops at once and exits with code **0**.

**Starting properly.** After an update, the new firmware must print a `healthy` or `degraded` status within 5 seconds; otherwise the agent restarts the old version and reports the update as failed.

**Crashing.** Any exit the agent did not ask for, or any non-zero exit code, is a crash. The agent should report it (see below) and restart the firmware after a short delay. The container stays up, so the orchestrator can tell a firmware crash (heartbeats continue, unhealthy) from an offline device (heartbeats stop).

**Settings** (environment variables, mainly for tests):

| Variable | Default | Used by |
|---|---|---|
| `FIRMWARE_TICK_SECONDS` | `1` | all versions |
| `FIRMWARE_FAULT_START_SECONDS` | `10` | 2.1: when errors start |
| `FIRMWARE_FAULT_CRASH_AFTER_SECONDS` | `5` | 2.1: how long after that it crashes |
| `FIRMWARE_FAULT_ERRORS_PER_TICK` | `5` | 2.1 |
| `FIRMWARE_STATE_DIR` | (none: state kept in memory only) | all versions; set by the agent |

**Reporting crashes.** The agent counts crashes separately from errors, in the heartbeat's `crash_count` field (crashes since the previous heartbeat), and reports `status` `unhealthy` while the firmware is down. A stop the agent asked for (update, rollback, shutdown) is not a crash. See section 4.1 of `deploy/mqtt/mqtt-topics-and-messages.md`.

## State and rollback

Firmware on a real device keeps data between runs: settings, calibration, counters. When an update changes the format of that data, rolling back is only safe if the old firmware gets data it can read. This is the policy every firmware version and the agent follow.

**The rules**

1. **One state folder per device.** Firmware keeps its persistent data in `FIRMWARE_STATE_DIR` (on the device: `~/firmware/state/`), never in its own install folder, which is replaced on every update.
2. **Every state is stamped with a schema.** The demo firmware keeps `config.json` with a `state_schema` number. A firmware declares the schema it writes with `STATE_SCHEMA = <n>` in `main.py`; the release tool copies it into the signed manifest as `state_schema`.
3. **Firmware converts forward, never back.** A firmware reads its own schema and older ones, converting older state on its first start (2.0 turns schema 1's flat `interval_seconds` into a `settings` block with a new `units` field). It **refuses** state newer than it understands: it logs `cannot read state schema N` and exits with code 2, rather than misreading it.
4. **Unknown fields are kept.** Firmware never drops fields it does not know, so data from other features survives.
5. **The agent snapshots before every switch.** With the old firmware stopped, the state folder is copied into the folder of the version being left (`versions/<old>/state-snapshot/`). Snapshots are kept for the current and previous versions only.
6. **Rollback restores the snapshot.** On a rollback, or any switch to firmware with an older `state_schema`, the agent restores the target version's snapshot before starting it. The old firmware sees exactly the state it had before the update.
7. **A failed start restores too.** If new firmware does not start properly, the agent restores the old version's snapshot before restarting it, since the new firmware may already have converted the state.
8. **No snapshot, no downgrade.** If the target uses an older schema and no snapshot exists, the agent refuses with `rollback_failed` ("no state snapshot for 1.0, which cannot read state schema 2") and keeps the current version running, instead of starting firmware that would crash.

**The trade-off.** Changes made to the state while the newer version ran are not carried back: a rollback returns the device to exactly where it was before the update. For a rollback after a failed or faulty update that is the safe choice. Keeping such changes would need a down-conversion in every newer version, and a bug in that converter would break the rollback itself.

**Firmware without state** declares no `STATE_SCHEMA`; the agent never treats moving to or from it as a downgrade.

**Example** (what the demo shows in steps 7 and 8):

| Moment | `config.json` | Who wrote it |
|---|---|---|
| Device on 1.0 | `{"state_schema": 1, "interval_seconds": 1.0, "boot_count": 1}` | 1.0 |
| Update to 2.0 | snapshot of the line above saved in `versions/1.0/state-snapshot/` | agent |
| 2.0 running | `{"state_schema": 2, "settings": {"interval_seconds": 1.0, "units": "metric"}, "boot_count": 2}` | 2.0 (converted) |
| Rollback to 1.0 | snapshot restored: `{"state_schema": 1, "interval_seconds": 1.0, "boot_count": 1}`, then 1.0 boots on it (`boot_count` 2) | agent, then 1.0 |

Without step 6, 1.0 would read schema 2 and exit with code 2 on every start: the rollback would fail. The tests prove both sides: `tests/component/test_firmware.py` (1.0 refuses schema 2, 2.0 converts schema 1) and `tests/component/test_device.py` (rollback restores, a failed start restores, no snapshot refuses).

## Packages

`tools/release.py` turns a folder into three files in `deploy/registries/software/packages/<version>/`:

| File | Contents |
|---|---|
| `firmware.tar.gz` | The folder's files (`main.py`), packed reproducibly: the same source always gives the same bytes and SHA-256 |
| `manifest.json` | `schema_version`, `version`, `filename`, `entrypoint`, `size_bytes`, `sha256`, `notes` (from the folder's `NOTES` file), and `state_schema` (from `STATE_SCHEMA` in `main.py`, if declared) |
| `manifest.sig` | Ed25519 signature over the exact bytes of `manifest.json`, base64 on one line |

The device checks a package in this order: signature, manifest version, manifest checksum against the command, package size and checksum. See section 4.3 of `deploy/mqtt/mqtt-topics-and-messages.md`; `verify_package()` in `tools/release.py` is the reference implementation.

## Keys

| File | Where | Committed |
|---|---|---|
| `keys/release-private.pem` | Release machine only | **No** (git-ignored). Back it up: losing it means re-signing everything with a new key and rebuilding the device image |
| `keys/release-public.pem` | Repository | Yes |
| `deploy/device-agent/release-public.pem` | Copied into the device image | Yes, kept identical by `release.py keygen` and checked by a test |

## Releasing

From the repository root, with `pip install -r tools/requirements.txt`:

```bash
python tools/release.py keygen          # once per project; refuses to replace an existing key
python tools/release.py build --all     # or: build 2.0
python tools/release.py verify 1.0 2.0 2.1
```

Commit the changed `packages/` folders. After editing anything under `firmware/`, rebuild: the test `test_committed_packages_match_source` fails until you do.

To add a version, create `firmware/<version>/main.py` with `VERSION = "<version>"`, `STATE_SCHEMA = <n>` if it keeps state (raise it whenever the state format changes, and convert older schemas forward), and a one-line `NOTES` file, then build it. The build refuses if the folder name and `VERSION` disagree.
