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
    crash_count       INTEGER NOT NULL DEFAULT 0,    -- firmware crashes since last report
    cpu_percent       REAL,                          -- container CPU, percent of one core
    memory_mb         REAL,                          -- container memory, MiB
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
    crash_count      INTEGER NOT NULL DEFAULT 0,
    cpu_percent      REAL,
    memory_mb        REAL,
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
    current_stage    INTEGER NOT NULL DEFAULT 0,        -- stage being rolled out (1 = first)
    stage_count      INTEGER NOT NULL DEFAULT 1,        -- number of stages in the plan
    stage_plan       TEXT,                              -- as given, e.g. '1,3,6' or '10%,50%,100%'
    halt_reason      TEXT,
    started_at       TIMESTAMPTZ,
    ended_at         TIMESTAMPTZ,
    -- Health limits this rollout is judged by (NULL: the orchestrator's defaults).
    health_window_seconds INTEGER,      -- watch each device this long after it updates
    max_crashes           INTEGER,      -- firmware crashes allowed per device in the window
    max_errors            INTEGER,      -- firmware errors allowed per device in the window
    max_failed_devices    INTEGER,      -- failed installs allowed before the rollout halts
    unresponsive_seconds  INTEGER       -- silence after updating that counts as unresponsive
);

-- Per-device outcome within a rollout. Used for "percent of fleet exposed".
CREATE TABLE rollout_devices (
    rollout_id  BIGINT NOT NULL REFERENCES rollouts(id) ON DELETE CASCADE,
    device_id   TEXT   NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    state       TEXT   NOT NULL DEFAULT 'pending'
        CONSTRAINT rollout_devices_state_check
        CHECK (state IN ('pending', 'commanded', 'updated', 'failed', 'rolled_back', 'skipped')),
    stage       INTEGER NOT NULL DEFAULT 1,             -- the stage this device is updated in
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_started_at TIMESTAMPTZ,      -- update_succeeded arrived: health window starts
    verdict     TEXT                    -- health evaluation result (rollout_logic.py)
        CONSTRAINT rollout_devices_verdict_check
        CHECK (verdict IN ('healthy', 'unhealthy', 'unresponsive', 'failed_update',
                           'no_reply', 'not_reached')),
    verdict_reason TEXT,
    verdict_at  TIMESTAMPTZ,
    PRIMARY KEY (rollout_id, device_id)
);

-- Decision log: every advance / halt / rollback with its reason.
CREATE TABLE rollout_events (
    id          BIGSERIAL PRIMARY KEY,
    rollout_id  BIGINT NOT NULL REFERENCES rollouts(id) ON DELETE CASCADE,
    event       TEXT NOT NULL
        CONSTRAINT rollout_events_event_check
        CHECK (event IN ('started', 'stage_advanced', 'halted', 'rollback_started',
                         'rollback_completed', 'completed', 'device_verdict')),
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
       crash_count,
       cpu_percent,
       memory_mb,
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

-- Placeholder baseline. The orchestrator replaces it at startup with the software
-- registry's versions and checksums (and adds the others).
INSERT INTO firmware_versions (version, notes) VALUES
    ('1.0', 'baseline (replaced from the software registry when the orchestrator starts)');

-- ---------------------------------------------------------------------------
-- Example: how the orchestrator records a heartbeat (psycopg / SQLAlchemy).
-- Upsert the device's current state, then append to history:
--
--   INSERT INTO devices (device_id, current_version, status, error_count, crash_count, last_heartbeat)
--   VALUES (%(id)s, %(version)s, %(status)s, %(errors)s, %(crashes)s, now())
--   ON CONFLICT (device_id) DO UPDATE
--     SET current_version = EXCLUDED.current_version,
--         status          = EXCLUDED.status,
--         error_count     = EXCLUDED.error_count,
--         crash_count     = EXCLUDED.crash_count,
--         last_heartbeat  = EXCLUDED.last_heartbeat,
--         updated_at      = now();
--
--   INSERT INTO heartbeats (device_id, reported_version, status, error_count, crash_count, reported_at)
--   VALUES (%(id)s, %(version)s, %(status)s, %(errors)s, %(crashes)s, %(device_ts)s);
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- Messages the orchestrator rejected (invalid_message) and forged commands devices refused
-- (refused_command). No foreign key: the device may not be registered.
CREATE TABLE rejected_messages (
    id          BIGSERIAL PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    device_id   TEXT NOT NULL,
    topic       TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('invalid_message', 'refused_command')),
    reason      TEXT NOT NULL,
    payload     TEXT                       -- the start of the message, for investigation
);
CREATE INDEX rejected_messages_time_idx ON rejected_messages (received_at DESC);

-- CPU and memory of each control-plane container, sampled by the stats collector.
CREATE TABLE control_plane_stats (
    id          BIGSERIAL PRIMARY KEY,
    sampled_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    service     TEXT NOT NULL,             -- Compose service name
    cpu_percent REAL NOT NULL,             -- percent of one core
    memory_mb   REAL NOT NULL
);
CREATE INDEX control_plane_stats_time_idx ON control_plane_stats (sampled_at DESC);

