# Device Agent (simulated IoT device)

Each container is one simulated device. In Iteration 1 it connects to the MQTT broker and publishes a heartbeat every 5 seconds. Update and rollback handling arrives in Iteration 2.

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

## Add more devices

Copy the `device-001` block in `docker-compose.yml`, change the service name and `DEVICE_ID`, and run `docker compose up -d`. Two devices with the same ID disconnect each other.

## Notes

- `MQTT_HOST=host.docker.internal` reaches a broker published on the host machine. The `extra_hosts` line makes this work on Linux and WSL as well as Docker Desktop.
- If the broker is not reachable, the agent logs `Cannot reach broker` and keeps retrying. If it loses the connection later, it reconnects by itself.
- Run it without Docker for quick tests: `DEVICE_ID=device-001 python agent.py`