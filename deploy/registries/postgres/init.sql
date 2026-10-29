-- init.sql: schema for the OTA Fleet Manager registries.
--
-- Mounted into the postgres container at /docker-entrypoint-initdb.d/, so it runs
-- ONCE, the first time the database volume is created. To re-run it after changes:
--   docker compose down -v && docker compose up -d      (this deletes all data)
--
-- Iteration 1 needs: firmware_versions, devices, heartbeats.
-- The rollout tables are here now so the schema does not have to change in
-- Iterations 2-3. They stay empty until the orchestrator uses them.

-- ---------------------------------------------------------------------------
-- Software registry metadata: one row per firmware version available to deploy.
-- The package files themselves live in the file server; this table records them.
-- ---------------------------------------------------------------------------
CREATE TABLE firmware_versions (
    version        TEXT PRIMARY KEY,                 -- e.g. '1.0', '2.0', '2.1'
    checksum_sha256 TEXT,                            -- verified by the agent before install
    package_path   TEXT,                             -- path/URL on the software registry
    notes          TEXT,                             -- e.g. 'healthy' or 'faulty: crashes on start'
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Rollout groups: the stage in which a device receives an update.
-- ---------------------------------------------------------------------------
CREATE TABLE rollout_groups (
    name         TEXT PRIMARY KEY,
    stage_order  INTEGER NOT NULL UNIQUE             -- 1 = first stage
);

-- ---------------------------------------------------------------------------
-- Device registry: current state of every device (one row per device).
-- ---------------------------------------------------------------------------
CREATE TABLE devices (
    device_id         TEXT PRIMARY KEY,              -- e.g. 'device-001'
    current_version   TEXT,                          -- last version the device reported
    previous_version  TEXT,                          -- kept on the device for rollback
    status            TEXT NOT NULL DEFAULT 'unknown'
        CHECK (status IN ('unknown', 'healthy', 'degraded', 'unhealthy', 'updating', 'offline')),
    error_count       INTEGER NOT NULL DEFAULT 0,    -- errors since last report/restart
    rollout_group     TEXT REFERENCES rollout_groups(name),
    last_heartbeat    TIMESTAMPTZ,
    registered_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX devices_version_idx ON devices (current_version);
CREATE INDEX devices_group_idx   ON devices (rollout_group);

-- ---------------------------------------------------------------------------
-- Heartbeat history: every report received. Drives Grafana time-series charts
-- and the experiment measurements (time to detect, time to roll back).
-- ---------------------------------------------------------------------------
CREATE TABLE heartbeats (
    id               BIGSERIAL PRIMARY KEY,
    device_id        TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    reported_version TEXT,
    status           TEXT,
    error_count      INTEGER,
    reported_at      TIMESTAMPTZ,                    -- timestamp from the device
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now()  -- timestamp at the orchestrator
);

CREATE INDEX heartbeats_device_time_idx ON heartbeats (device_id, received_at DESC);
CREATE INDEX heartbeats_time_idx        ON heartbeats (received_at DESC);

-- ---------------------------------------------------------------------------
-- Rollouts (used from Iteration 2): one row per release of a version to the fleet.
-- ---------------------------------------------------------------------------
CREATE TABLE rollouts (
    id               BIGSERIAL PRIMARY KEY,
    target_version   TEXT NOT NULL REFERENCES firmware_versions(version),
    rollback_version TEXT NOT NULL REFERENCES firmware_versions(version),  -- last stable
    state            TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'in_progress', 'completed', 'halted', 'rolled_back')),
    current_stage    INTEGER NOT NULL DEFAULT 0,
    halt_reason      TEXT,
    started_at       TIMESTAMPTZ,
    ended_at         TIMESTAMPTZ
);

-- Per-device outcome within a rollout. Used for "percent of fleet exposed".
CREATE TABLE rollout_devices (
    rollout_id  BIGINT NOT NULL REFERENCES rollouts(id) ON DELETE CASCADE,
    device_id   TEXT   NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    state       TEXT   NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'commanded', 'updated', 'failed', 'rolled_back')),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (rollout_id, device_id)
);

-- Decision log: every advance / halt / rollback with its reason.
CREATE TABLE rollout_events (
    id          BIGSERIAL PRIMARY KEY,
    rollout_id  BIGINT NOT NULL REFERENCES rollouts(id) ON DELETE CASCADE,
    event       TEXT NOT NULL
        CHECK (event IN ('started', 'stage_advanced', 'halted', 'rollback_started',
                         'rollback_completed', 'completed')),
    stage       INTEGER,
    reason      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX rollout_events_rollout_idx ON rollout_events (rollout_id, created_at);

-- ---------------------------------------------------------------------------
-- Convenience view for the dashboard and the orchestrator's liveness checks.
-- The orchestrator decides what counts as "offline"; this just exposes the age.
-- ---------------------------------------------------------------------------
CREATE VIEW fleet_overview AS
SELECT device_id,
       current_version,
       status,
       error_count,
       rollout_group,
       last_heartbeat,
       EXTRACT(EPOCH FROM (now() - last_heartbeat))::INTEGER AS seconds_since_heartbeat
FROM devices;

-- ---------------------------------------------------------------------------
-- Seed data
-- ---------------------------------------------------------------------------
INSERT INTO rollout_groups (name, stage_order) VALUES
    ('canary',    1),
    ('stage_10',  2),
    ('stage_50',  3),
    ('stage_100', 4);

-- Placeholder baseline so devices reporting '1.0' in Iteration 1 match a known version.
INSERT INTO firmware_versions (version, notes) VALUES
    ('1.0', 'baseline (placeholder until packages exist in Iteration 2)');

-- ---------------------------------------------------------------------------
-- Example: how the orchestrator records a heartbeat (psycopg / SQLAlchemy).
-- Upsert the device's current state, then append to history:
--
--   INSERT INTO devices (device_id, current_version, status, error_count, last_heartbeat)
--   VALUES (%(id)s, %(version)s, %(status)s, %(errors)s, now())
--   ON CONFLICT (device_id) DO UPDATE
--     SET current_version = EXCLUDED.current_version,
--         status          = EXCLUDED.status,
--         error_count     = EXCLUDED.error_count,
--         last_heartbeat  = EXCLUDED.last_heartbeat,
--         updated_at      = now();
--
--   INSERT INTO heartbeats (device_id, reported_version, status, error_count, reported_at)
--   VALUES (%(id)s, %(version)s, %(status)s, %(errors)s, %(device_ts)s);
-- ---------------------------------------------------------------------------