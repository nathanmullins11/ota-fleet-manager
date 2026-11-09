-- Adds crash_count to a database created before it existed.
-- init.sql already includes these changes, so fresh databases do not need this.
-- Safe to run more than once.
--
--   docker compose exec -T postgres psql -U ota -d ota < registries/postgres/migrations/001_crash_count.sql

BEGIN;

ALTER TABLE devices    ADD COLUMN IF NOT EXISTS crash_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE heartbeats ADD COLUMN IF NOT EXISTS crash_count INTEGER NOT NULL DEFAULT 0;

-- Recreated rather than replaced: CREATE OR REPLACE VIEW can only add columns at the end.
DROP VIEW IF EXISTS fleet_overview;
CREATE VIEW fleet_overview AS
SELECT device_id,
       current_version,
       status,
       error_count,
       crash_count,
       rollout_group,
       last_heartbeat,
       EXTRACT(EPOCH FROM (now() - last_heartbeat))::INTEGER AS seconds_since_heartbeat
FROM devices;

COMMIT;
