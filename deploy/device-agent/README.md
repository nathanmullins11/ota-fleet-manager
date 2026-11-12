# Device Agent (simulated IoT device)

Each container is one simulated device. It runs the installed firmware as a child process, publishes a heartbeat with the firmware's health every 5 seconds, and carries out update and rollback commands from the orchestrator. To run many devices at once, see [Run a fleet](#run-a-fleet-10-to-50-devices).

The message format is defined in `../../docs/mqtt-topics-and-messages.md`.

## Run

```bash
cp .env.example .env          # set MQTT_HOST (see the file for options)
docker compose up -d --build
docker compose logs -f --tail 50 device-001
```

Expected log:

```
Installing factory firmware 1.0
Firmware 1.0 started (pid 8)
firmware: firmware 1.0 started
Starting device-001 (firmware 1.0, heartbeat every 5.0s)
Connected to broker host.docker.internal:1883 as device-001
Publishing heartbeats to ota/devices/device-001/heartbeat
```

After a restart of the same container, the first line is missing: the installed version is kept.

Then check the registry (from `deploy/`, with the control-plane stack and broker running):

```bash
docker compose exec postgres psql -U ota -d ota -c "SELECT * FROM fleet_overview;"
```

`device-001` should appear with version `1.0` and `seconds_since_heartbeat` between 0 and 5.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `DEVICE_ID` | (required) | Like `device-001`. Also the MQTT client ID, so it must be unique per device |
| `MQTT_HOST` | `localhost` (`host.docker.internal` in the Compose file) | Broker address |
| `MQTT_PORT` | `1883` | Broker port |
| `HEARTBEAT_INTERVAL_SECONDS` | `5` | Time between heartbeats |
| `LOG_LEVEL` | `INFO` | Use `DEBUG` to log every heartbeat sent |
| `FIRMWARE_START_TIMEOUT_SECONDS` | `5` | New firmware must report a healthy status within this time, or the update fails and the old version is restarted |
| `FIRMWARE_RESTART_DELAY_SECONDS` | `2` | Wait before restarting firmware that crashed |
| `DOWNLOAD_TIMEOUT_SECONDS` | `15` | Per file, when downloading a package |
| `FIRMWARE_DIR` | `~/firmware` | Installed versions and `state.json` |

The firmware's own settings (`FIRMWARE_TICK_SECONDS`, `FIRMWARE_FAULT_*`, see `firmware/README.md`) are passed through to it. The version a device runs is no longer a setting: it is whatever is installed, starting with the factory firmware 1.0.

## How updates work

| Part | File | Job |
|---|---|---|
| Firmware runner | `firmware_runner.py` | Runs `main.py` of the installed version, reads its status lines, counts errors and crashes, restarts it after a crash |
| Installer | `installer.py` | Downloads and verifies packages, unpacks them, keeps the current and previous versions |
| Commands | `commands.py` | Checks that a command is well formed |
| Agent | `agent.py` | Heartbeats, events, and a worker that handles commands one at a time |

**First start.** The image contains the signed 1.0 package (`factory/1.0/`, a copy of the registry's, kept identical by `tools/release.py build 1.0` and a test). It is verified and installed into `~/firmware`, which survives container restarts but not removal. At every start the installed copy is verified again before it runs; if it fails, the previous version (or the factory package) is used instead.

**Update command.**
1. If the target is already running: answer `update_succeeded` ("already running 2.0") and do nothing else. Repeated or redelivered commands are harmless.
2. Send `update_started`. Heartbeats say `updating` until the command is finished.
3. Download the manifest and signature, check the signature, the version, and the checksum against the command; only then download the package and check its size and checksum (mqtt-topics-and-messages.md 4.3). The first failed check ends the update with `update_failed` and that check as the reason.
4. Unpack, stop the old firmware (not a crash), start the new one. If it reports a healthy or degraded status within 5 s, the new version becomes current: `update_succeeded`. If it exits or stays silent, the old version is restarted: `update_failed` ("firmware 9.0 exited with code 3 before reporting its status; still running 1.0"). Such a failed start is not counted as a crash.

**Rollback command.** The same, with `rollback_*` events. The previous version's package is kept on the device, so it is reinstalled from there after verifying it against the command's checksum; it is downloaded only if the copy is missing or fails verification.

**Commands arriving during an update** wait in a queue and run in order afterwards. A malformed command that still names a rollout and version is answered with a failure event ("invalid command: ..."); one that does not is only logged.

**Health in the heartbeat.** `status` is the worst the firmware reported since the previous heartbeat (`unhealthy` if it crashed in that time or is waiting to be restarted), `error_count` the sum of its per-tick errors, `crash_count` the number of crashes. A heartbeat skipped while the broker is unreachable keeps its counts for the next one.

The agent never rolls back by itself because of crashes or errors: it reports them, and deciding when a rollout is unhealthy is the orchestrator's job (FR-4).

## Run a fleet (10 to 50 devices)

`fleet.sh` starts a fleet of devices from the same image and settings (`.env`), each with a unique ID (`device-001`, `device-002`, …) that is also its MQTT client ID:

```bash
./fleet.sh up 10                 # start device-001 … device-010; the fleet becomes exactly 10 devices
./fleet.sh status                # each device: container state and broker connection
./fleet.sh stop device-004       # stop one device: offline after the 30 s heartbeat timeout
./fleet.sh start device-004      # bring it back
./fleet.sh logs device-004 -f    # follow one device's log
./fleet.sh stats --duration 60   # CPU and memory per device and in total, saved as CSV in fleet-stats/
./fleet.sh down                  # remove the whole fleet
```

- **Batches.** Devices start 5 at a time with a 2 s pause (`--batch`, `--pause`), so the broker is not hit by every connection at once. `up` waits until every device has connected and lists any that did not, with their last log line.
- **Size.** The default is 10. Up to 50 is allowed (NF-4); more needs `--force` (scale testing in Iteration 4). Running `up` with a smaller number removes the devices above it; running it again with the same number starts only what is missing.
- **Broker.** `MQTT_HOST` and `MQTT_PORT` come from `.env` (or `--host`, `--port`), so the same command works for a local control plane and the cloud VM.
- **One launch path at a time.** It refuses to start while the demo's devices or this folder's Compose `device-001` are running: two devices with the same ID keep disconnecting each other.

Fleet containers are named `ota-fleet-device-NNN`, carry the label `ota-fleet=device`, restart by themselves if they crash (not after `stop`), and keep at most 10 MB of logs each.

### Measured resource use

On the development machine (12 CPUs, 7.6 GB RAM, Docker Desktop on WSL2), with the control plane on the same machine, 30 s of samples each (`fleet.sh stats`):

| Devices | Registered and healthy | Total CPU | Total memory | Per device |
|---|---|---|---|---|
| 10 | 10 / 10 | 0.2 % of one core | 133 MiB | 13.3 MiB |
| 25 | 25 / 25 | 0.6 % | 335 MiB | 13.4 MiB |
| 50 | 50 / 50 | 1.1 % | 667 MiB | 13.3 MiB |

Memory grows linearly at about 13 MiB per device and CPU is negligible, so 50 devices fit easily on a laptop. At 50 devices the control plane used about 1 % CPU for the orchestrator and under 40 MiB per container. These figures predate the agent running firmware as a child process (a second Python process per device), and that roughly doubles memory: about 25 MiB per device at the end of a full local demo run (5 devices, 124.5 MiB in total). Re-measure 10, 25 and 50 devices with `fleet.sh stats`, and in Iteration 4 on the cloud VM setup.

## Add a device to the Compose file

The Compose file (see [Run](#run)) holds one device. To add one by hand, copy the `device-001` block, change the service name and `DEVICE_ID`, and run `docker compose up -d`. For more than a couple of devices, use `fleet.sh`.

## Notes

- `MQTT_HOST=host.docker.internal` reaches a broker published on the host machine. The `extra_hosts` line makes this work on Linux and WSL as well as Docker Desktop.
- If the broker is not reachable, the agent logs `Cannot reach broker` and keeps retrying. If it loses the connection later, it reconnects by itself.
- Run it without Docker for quick tests: `DEVICE_ID=device-001 FIRMWARE_DIR=/tmp/fw python agent.py` (needs `pip install -r requirements.txt`).