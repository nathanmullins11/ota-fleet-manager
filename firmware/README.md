# Firmware

The firmware that devices install over the air. Each folder is one version:

| Version | Behavior | Purpose |
|---|---|---|
| `1.0` | Always healthy | Baseline every device starts on |
| `2.0` | Always healthy, also reports `uptime_seconds` | The good update |
| `2.1` | Healthy for 10 s, then 5 errors per tick for 5 s, then crashes (exit 1) | The bad update that should trigger rollback |

2.1 fails the same way on every run so that experiments are repeatable. The agent restarts it after each crash, so it keeps cycling until the device is rolled back.

## Interface between the device agent and the firmware

This is the contract the device agent codes against.

**Starting.** The agent unpacks a package into its own folder and runs `python main.py` (the manifest's `entrypoint`) with that folder as the working directory, as a child process. Firmware uses only the Python standard library.

**Status output.** Once per tick (1 second by default) the firmware prints one JSON object per line to **stdout**:

```json
{"version": "2.0", "status": "healthy", "errors": 0, "uptime_seconds": 12}
```

| Field | Type | Meaning |
|---|---|---|
| `version` | string | The firmware's own version |
| `status` | string | `healthy`, `degraded` or `unhealthy` |
| `errors` | integer | Errors during this tick (not a running total) |

Other fields may appear and should be ignored. For the heartbeat, the agent reports the worst `status` and the sum of `errors` since the previous heartbeat. **stderr** is for human-readable log lines only (the agent copies them into its own log).

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

**Reporting crashes.** The agent counts crashes separately from errors, in the heartbeat's `crash_count` field (crashes since the previous heartbeat), and reports `status` `unhealthy` while the firmware is down. A stop the agent asked for (update, rollback, shutdown) is not a crash. See section 4.1 of `deploy/mqtt/mqtt-topics-and-messages.md`.

## Packages

`tools/release.py` turns a folder into three files in `deploy/registries/software/packages/<version>/`:

| File | Contents |
|---|---|
| `firmware.tar.gz` | The folder's files (`main.py`), packed reproducibly: the same source always gives the same bytes and SHA-256 |
| `manifest.json` | `schema_version`, `version`, `filename`, `entrypoint`, `size_bytes`, `sha256`, `notes` (from the folder's `NOTES` file) |
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

To add a version, create `firmware/<version>/main.py` with `VERSION = "<version>"` and a one-line `NOTES` file, then build it. The build refuses if the folder name and `VERSION` disagree.
