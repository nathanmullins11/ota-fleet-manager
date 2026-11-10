# Device Agent (simulated IoT device)

Each container is one simulated device. It connects to the MQTT broker and publishes a heartbeat every 5 seconds. Update and rollback handling arrives with the device update logic (Iteration 2). To run many devices at once, see [Run a fleet](#run-a-fleet-10-to-50-devices).

The message format is defined in `../../docs/mqtt-topics-and-messages.md`.

## Run

```bash
cp .env.example .env          # set MQTT_HOST (see the file for options)
docker compose up -d --build
docker compose logs -f --tail 50 device-001
```

Expected log:

```
Starting device-001 (firmware 1.0, heartbeat every 5.0s)
Connected to broker host.docker.internal:1883 as device-001
Publishing heartbeats to ota/devices/device-001/heartbeat
```

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
| `FIRMWARE_VERSION` | `1.0` | Version reported in each heartbeat |
| `HEARTBEAT_INTERVAL_SECONDS` | `5` | Time between heartbeats |
| `LOG_LEVEL` | `INFO` | Use `DEBUG` to log every heartbeat sent |

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

Memory grows linearly at about 13 MiB per device and CPU is negligible, so 50 devices fit easily on a laptop. At 50 devices the control plane used about 1 % CPU for the orchestrator and under 40 MiB per container. Expect roughly double the memory per device once the agent runs firmware as a child process (a second Python process per device). Re-measure then, and in Iteration 4 on the cloud VM setup.

## Add a device to the Compose file

The Compose file (see [Run](#run)) holds one device. To add one by hand, copy the `device-001` block, change the service name and `DEVICE_ID`, and run `docker compose up -d`. For more than a couple of devices, use `fleet.sh`.

## Notes

- `MQTT_HOST=host.docker.internal` reaches a broker published on the host machine. The `extra_hosts` line makes this work on Linux and WSL as well as Docker Desktop.
- If the broker is not reachable, the agent logs `Cannot reach broker` and keeps retrying. If it loses the connection later, it reconnects by itself.
- Run it without Docker for quick tests: `DEVICE_ID=device-001 python agent.py`