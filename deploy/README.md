# Control Plane: Running and Testing

This folder holds the Docker Compose setup for the OTA Fleet Manager control plane. It runs four services:

| Service | What it is | Port |
|---|---|---|
| `mosquitto` | MQTT broker (Eclipse Mosquitto 2, configured by `mqtt/mosquitto.conf`) | 1883 |
| `postgres` | Device registry (PostgreSQL 16, schema loaded from `registries/postgres/init.sql`) | 5432 (localhost only) |
| `software-registry` | Serves versioned firmware packages (FastAPI) | 8080 |
| `orchestrator` | Listens for device heartbeats over MQTT and records them in `postgres` | none |

Grafana (the dashboard) runs in the same Compose file, on port 3000. The broker accepts anonymous, unencrypted connections for now; TLS and per-device credentials arrive in Iteration 3. Until then, limit port 1883 on the cloud VM to known IP addresses.

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
curl -o firmware-2.0.tar.gz localhost:8080/packages/2.0   # downloads the package
curl localhost:8080/packages/2.0/manifest                  # the signed manifest
curl localhost:8080/packages/2.0/manifest.sig              # the signature over it
```

Expected: `/versions` lists `1.0`, `2.0`, and `2.1`, each with a SHA-256 checksum, notes, and its download, manifest and signature URLs. A download includes an `X-Checksum-SHA256` header (add `-i` to `curl` to see it).

To check the published packages exactly as a device will (signature, version, checksum), run this from the repository root on any machine with `pip install -r tools/requirements.txt`:

```bash
python tools/release.py verify 1.0 2.0 2.1 --registry http://localhost:8080
```

You can also open `http://localhost:8080/docs` in a browser for an interactive list of the endpoints.

Each version is a folder in `registries/software/packages/` holding `firmware.tar.gz`, `manifest.json` and `manifest.sig`. The registry serves only complete folders; `/health` returns 503 and names any folder that is missing a file or has a manifest for a different version. Versions are built and signed with `tools/release.py` (see `firmware/README.md`). No restart or rebuild is needed after adding one.

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

## Orchestrator

The orchestrator connects to the MQTT broker, records heartbeats in the device registry, marks devices `offline` when their heartbeats stop, and runs rollouts: it sends update and rollback commands and records what the devices report. At startup it copies the software registry's versions and checksums into `firmware_versions`. Compose starts it after `mosquitto`, `postgres` and `software-registry` report healthy.

To test it without a device, publish a heartbeat by hand:

```bash
docker compose exec mosquitto mosquitto_pub -t ota/devices/device-001/heartbeat -m '{"schema_version":1,"device_id":"device-001","version":"1.0","status":"healthy","error_count":0,"timestamp":"2026-10-29T14:03:07Z"}'
docker compose exec postgres psql -U ota -d ota -c "SELECT * FROM fleet_overview;"
```

Expected: `device-001` appears with its version and a recent heartbeat. Stop publishing and after 30 seconds the orchestrator logs `Device device-001 marked offline` and the status changes to `offline`. Malformed messages are rejected with a warning that names the reason (`docker compose logs orchestrator`).

To test with a simulated device, see `device-agent/README.md`.

Settings are environment variables (see `orchestrator/config.py` and `.env.example`): `MQTT_HOST`, `DB_HOST`, `HEARTBEAT_TIMEOUT_SECONDS`, `REGISTRY_PUBLIC_URL`, `COMMAND_TIMEOUT_SECONDS`, `LOG_LEVEL`, and others.

## Dashboard

Open **http://localhost:3000** (on the cloud VM: `http://<elastic-ip>:3000`, with port 3000 open to your IP). It is live (refreshes every 5 seconds) and view-only: anyone who can reach the port can watch, and no login is needed. Rollouts are still started and rolled back from the CLI below or the cloud live view.

| Section | Shows |
|---|---|
| Fleet now | Devices, healthy, unhealthy, offline; every device's version, status, errors, crashes and last heartbeat; devices per firmware version |
| Health over time | Firmware errors and crashes per device, and how many devices are reporting |
| Rollout | The selected rollout (default: the latest): state, versions, how much of the fleet it reached, each device's health verdict and reason, and the decision log |
| Rollout history | Recent rollouts with their duration, time to halt and time to roll back |
| Resources | CPU and memory of every device (from its heartbeats) and of every control-plane service (from the stats collector), now and over time |
| Security | Messages the orchestrator rejected, and forged commands devices refused, with the device and reason |

How it is set up:

- Everything comes from files in `grafana/`: the data source (`provisioning/datasources/postgres.yml`) and the dashboard (`dashboards/fleet.json`). Change the JSON, not the Grafana UI: changes made in the UI are not saved.
- Grafana reads the database through a **read-only login** (`dashboard_reader`) that the orchestrator creates or updates every time it starts. It can read every table and view, including ones added by later migrations, and change nothing.
- Settings in `.env`: `GRAFANA_ADMIN_PASSWORD` (the `admin` login, needed only to edit) and `GRAFANA_DB_PASSWORD` (the read-only login). `demo/cloud/control-plane.sh` generates both on the VM.
- **Device resource use** comes from the devices themselves (optional `cpu_percent` and `memory_mb` in each heartbeat), so it works when the devices run on another machine.
- **Control-plane resource use** comes from the `stats-collector` service, which reads container stats from the Docker socket every 5 seconds and writes them to `control_plane_stats` (kept 24 hours). The socket is mounted read-only and the collector only lists containers and reads their stats, but access to it is powerful: keep it in mind if you change that service.
- `tests/component/test_dashboard.py` runs every dashboard query as the read-only login against a real database, so a schema change that breaks a panel fails CI.

## Run a rollout (update, health check, automatic rollback)

Rollouts are started from the CLI inside the orchestrator container. The CLI only records the request in the database; the running orchestrator sends the commands within about a second, follows the devices' events, and logs every decision with its reason (`docker compose logs -f orchestrator`).

```bash
docker compose exec orchestrator python cli.py versions                  # firmware in the registry
docker compose exec orchestrator python cli.py devices                   # fleet, with previous versions
docker compose exec orchestrator python cli.py rollout start 2.0 --all   # or --devices device-001,device-002
docker compose exec orchestrator python cli.py rollout start 2.1 --all --max-errors 50 --health-window 30
docker compose exec orchestrator python cli.py rollout status --watch    # latest rollout, live
docker compose exec orchestrator python cli.py rollout rollback 1        # send rollout 1's devices back
docker compose exec orchestrator python cli.py rollout list
```

**Rules:**

- One rollout at a time. A new one is refused while another is `pending`, `in_progress` or `halted`.
- Offline devices are skipped (a command sent now would never reach them), and devices already on the target version get no command. Both are listed when the rollout is created and recorded in its `started` decision.
- The rollback version defaults to the version the commanded devices run now; if they run different versions, give it with `--rollback-to`.
- A device that does not answer a command within `COMMAND_TIMEOUT_SECONDS` (default 120) is marked `failed`, so a rollout always finishes. If it kept sending heartbeats it is `no_reply` (an agent problem, counted as a failed update); if it went silent it is `not_reached` (offline, never a reason to roll back).
- `rollback` works on `in_progress` and `completed` rollouts and sends every device in the rollout back to its rollback version. You can still roll back by hand; the health rules below do it automatically.

**Health evaluation and automatic rollback (FR-4, FR-5).** After a device reports `update_succeeded`, the orchestrator watches it for a health window and gives it a verdict, shown in `rollout status` and recorded in the decision log:

| Verdict | When | Rolls back? |
|---|---|---|
| `healthy` | The window passed within the limits, and the device is still heard from | No |
| `unhealthy` | More firmware crashes or errors than allowed within the window | **Yes** |
| `unresponsive` | Updated, then no heartbeat for longer than allowed (boot loop, broken agent) | **Yes** |
| `failed_update` / `no_reply` | Reported `update_failed`, or heartbeats but no reply to the command | **Yes**, once more devices than allowed fail |
| `not_reached` | No heartbeat and no reply after the command: offline | No |

The first verdict that crosses a limit halts the rollout and rolls every device in it back, with a reason like `automatic: device-003 unhealthy: crashed 1 time within 18 s of updating to 2.1 (limit 0)`. A rollout is `completed` only when every updated device has been judged `healthy`. A short drop in heartbeats inside the window is not a failure, as long as the device is back within the silence limit.

| Limit | Setting (default) | Per rollout |
|---|---|---|
| Health window | `HEALTH_WINDOW_SECONDS` (60) | `--health-window` |
| Crashes per device | `HEALTH_MAX_CRASHES` (0) | `--max-crashes` |
| Errors per device | `HEALTH_MAX_ERRORS` (10) | `--max-errors` |
| Failed installs | `HEALTH_MAX_FAILED_DEVICES` (0) | `--max-failed` |
| Silence after updating | `HEALTH_UNRESPONSIVE_SECONDS` (45) | `--unresponsive` |

The settings go in `.env`; the limits a rollout is judged by are stored with it, so its decisions can always be explained later. The rules are in `orchestrator/rollout_logic.py` (`evaluate_device`, `health_decision`).

Each device gets its own command on `ota/devices/{id}/commands`; there is no broadcast topic. Commands carry download URLs built from `REGISTRY_PUBLIC_URL`, the registry address **as the devices see it**: the default `http://host.docker.internal:8080` suits devices on the same machine; on the cloud VM set it to `http://<elastic-ip>:8080` in `.env` (`demo/cloud/control-plane.sh` does this for you).

Until the device agent installs firmware, you can play a device's part by publishing its events by hand:

```bash
docker compose exec mosquitto mosquitto_pub -q 1 -t ota/devices/device-001/events -m '{"schema_version":1,"device_id":"device-001","event":"update_succeeded","rollout_id":1,"version":"2.0","reason":null,"timestamp":"2026-11-05T12:00:00Z"}'
```

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