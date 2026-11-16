-- Dashboard data: device resource use from heartbeats, control-plane container resource use
-- (stats collector), and messages the system rejected (security panel).
-- init.sql already includes these changes, so fresh databases do not need this.
-- Safe to run more than once.
--
--   docker compose exec -T postgres psql -U ota -d ota < registries/postgres/migrations/003_resources_and_security.sql

BEGIN;

-- Device CPU (percent of one core) and memory (MiB) from heartbeats; NULL if not reported.
ALTER TABLE devices    ADD COLUMN IF NOT EXISTS cpu_percent REAL;
ALTER TABLE devices    ADD COLUMN IF NOT EXISTS memory_mb   REAL;
ALTER TABLE heartbeats ADD COLUMN IF NOT EXISTS cpu_percent REAL;
ALTER TABLE heartbeats ADD COLUMN IF NOT EXISTS memory_mb   REAL;

DROP VIEW IF EXISTS fleet_overview;
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

-- Messages the orchestrator rejected (invalid_message) and forged commands devices refused
-- (refused_command). No foreign key: the device may not be registered.
CREATE TABLE IF NOT EXISTS rejected_messages (
    id          BIGSERIAL PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    device_id   TEXT NOT NULL,
    topic       TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('invalid_message', 'refused_command')),
    reason      TEXT NOT NULL,
    payload     TEXT                       -- the start of the message, for investigation
);
CREATE INDEX IF NOT EXISTS rejected_messages_time_idx ON rejected_messages (received_at DESC);

-- CPU and memory of each control-plane container, sampled by the stats collector.
CREATE TABLE IF NOT EXISTS control_plane_stats (
    id          BIGSERIAL PRIMARY KEY,
    sampled_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    service     TEXT NOT NULL,             -- Compose service name
    cpu_percent REAL NOT NULL,             -- percent of one core
    memory_mb   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS control_plane_stats_time_idx ON control_plane_stats (sampled_at DESC);

COMMIT;
