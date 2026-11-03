# Control Plane: Running and Testing

This folder holds the Docker Compose setup for the OTA Fleet Manager control plane. It runs four services:

| Service | What it is | Port |
|---|---|---|
| `mosquitto` | MQTT broker (Eclipse Mosquitto 2, configured by `mqtt/mosquitto.conf`) | 1883 |
| `postgres` | Device registry (PostgreSQL 16, schema loaded from `registries/postgres/init.sql`) | 5432 (localhost only) |
| `software-registry` | Serves versioned firmware packages (FastAPI) | 8080 |
| `orchestrator` | Listens for device heartbeats over MQTT and records them in `postgres` | none |

Grafana is added to the same Compose file in a later task. The broker accepts anonymous, unencrypted connections for now; TLS and per-device credentials arrive in Iteration 3. Until then, limit port 1883 on the cloud VM to known IP addresses.

## Prerequisites

- Docker with the Compose plugin (`docker compose version` should work)
- Run all commands below from this `deploy/` folder

## Start

```bash
cp .env.example .env          # then edit POSTGRES_PASSWORD
docker compose up -d --build
docker compose ps             # mosquitto, postgres, and software-registry should show "healthy"; orchestrator has no health label
```

The first build downloads base images, so it takes a minute. If `.env` is missing or the password is empty, Compose stops with an error message on purpose.

## Test the broker

```bash
docker compose logs orchestrator | grep "Connected to broker"
docker compose exec mosquitto mosquitto_sub -t 'ota/devices/+/heartbeat' -v
```

Expected: the orchestrator logged `Connected to broker mosquitto:1883`. The second command waits and prints each heartbeat as it arrives (Ctrl+C to stop).

## Test the software registry

```bash
curl localhost:8080/health
curl localhost:8080/versions
curl localhost:8080/versions/2.1
curl -O localhost:8080/packages/2.0        # downloads the package
```

Expected: `/versions` lists `1.0`, `2.0`, and `2.1` with a SHA-256 checksum each. A download includes an `X-Checksum-SHA256` header (add `-i` to `curl` to see it).

You can also open `http://localhost:8080/docs` in a browser for an interactive list of the endpoints.

To add a version, drop a file named `<version>.<ext>` (for example `3.0.txt` or `3.0.tar.gz`) into `registries/software/packages/`. No restart or rebuild is needed.

## Test the device registry

List the tables and check the seed data:

```bash
docker compose exec postgres psql -U ota -d ota -c "\dt"
docker compose exec postgres psql -U ota -d ota -c "SELECT * FROM rollout_groups ORDER BY stage_order;"
docker compose exec postgres psql -U ota -d ota -c "SELECT * FROM firmware_versions;"
```

Expected: seven tables, four rollout groups (`canary`, `stage_10`, `stage_50`, `stage_100`), and one firmware version (`1.0`).

Simulate a heartbeat (this is the same upsert the orchestrator will run):

```bash
docker compose exec postgres psql -U ota -d ota -c "
INSERT INTO devices (device_id, current_version, status, error_count, last_heartbeat)
VALUES ('device-001', '1.0', 'healthy', 0, now())
ON CONFLICT (device_id) DO UPDATE
  SET current_version = EXCLUDED.current_version,
      status          = EXCLUDED.status,
      error_count     = EXCLUDED.error_count,
      last_heartbeat  = EXCLUDED.last_heartbeat,
      updated_at      = now();
INSERT INTO heartbeats (device_id, reported_version, status, error_count, reported_at)
VALUES ('device-001', '1.0', 'healthy', 0, now());"

docker compose exec postgres psql -U ota -d ota -c "SELECT * FROM fleet_overview;"
```

Expected: one row for `device-001` with `seconds_since_heartbeat` close to 0. Run the first statement again with `'degraded'` and `3` to see the row update in place while `heartbeats` gains a second row.

Check that bad data is rejected (this should fail with a constraint error):

```bash
docker compose exec postgres psql -U ota -d ota -c "INSERT INTO devices (device_id, status) VALUES ('x', 'bogus');"
```

For the schema design and example dashboard queries, see `registries/postgres/database-schema.md`.

## Orchestrator (skeleton)

The orchestrator connects to the MQTT broker, records heartbeats in the device registry, and marks devices `offline` when their heartbeats stop. Compose starts it after `mosquitto` and `postgres` report healthy.

To test it without a device, publish a heartbeat by hand:

```bash
docker compose exec mosquitto mosquitto_pub -t ota/devices/device-001/heartbeat -m '{"schema_version":1,"device_id":"device-001","version":"1.0","status":"healthy","error_count":0,"timestamp":"2026-10-29T14:03:07Z"}'
docker compose exec postgres psql -U ota -d ota -c "SELECT * FROM fleet_overview;"
```

Expected: `device-001` appears with its version and a recent heartbeat. Stop publishing and after 30 seconds the orchestrator logs `Device device-001 marked offline` and the status changes to `offline`. Malformed messages are rejected with a warning that names the reason (`docker compose logs orchestrator`).

To test with a simulated device, see `device-agent/README.md`.

Settings are environment variables (see `orchestrator/config.py`): `MQTT_HOST`, `DB_HOST`, `HEARTBEAT_TIMEOUT_SECONDS`, `LOG_LEVEL`, and others.

## Run the registry unit tests

These run without Docker:

```bash
cd software-registry
pip install -r requirements-dev.txt
pytest
```

## Stop and reset

```bash
docker compose down            # stop containers, keep the database data
docker compose down -v         # stop AND delete the database volume (fresh start)
```

`registries/postgres/init.sql` only runs when the database volume is created. After editing it, use `down -v` and then `up -d` to apply the changes. This deletes all data.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Set POSTGRES_PASSWORD in deploy/.env` | Create `.env` from `.env.example` and set a password |
| Port 5432 already in use | Another PostgreSQL is running locally. Change the mapping to `127.0.0.1:5433:5432` in `docker-compose.yml` |
| Port 1883 already in use | Another MQTT broker is running locally. Stop it, or change the mapping to `1884:1883` and set `MQTT_PORT=1884` for the devices |
| Port 8080 already in use | Change the mapping to `8081:8080` and use that port in the `curl` commands |
| Schema changes not showing up | The init script only runs on a new volume. Run `docker compose down -v`, then `up -d` |
| Service shows `unhealthy` | `docker compose logs <service>`, for example `docker compose logs mosquitto` |
| `permission denied` running Docker on Linux | Add your user to the `docker` group and log in again (`bootstrap.sh` does this on the VM) |