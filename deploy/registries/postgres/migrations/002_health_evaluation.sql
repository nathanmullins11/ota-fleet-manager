-- Health evaluation and automatic rollback (FR-4, FR-5): per-rollout health limits, a
-- per-device verdict, and 'device_verdict' entries in the decision log.
-- init.sql already includes these changes, so fresh databases do not need this.
-- Safe to run more than once.
--
--   docker compose exec -T postgres psql -U ota -d ota < registries/postgres/migrations/002_health_evaluation.sql

BEGIN;

-- The limits a rollout was judged by (NULL: the orchestrator's defaults at the time).
ALTER TABLE rollouts ADD COLUMN IF NOT EXISTS health_window_seconds INTEGER;
ALTER TABLE rollouts ADD COLUMN IF NOT EXISTS max_crashes           INTEGER;
ALTER TABLE rollouts ADD COLUMN IF NOT EXISTS max_errors            INTEGER;
ALTER TABLE rollouts ADD COLUMN IF NOT EXISTS max_failed_devices    INTEGER;
ALTER TABLE rollouts ADD COLUMN IF NOT EXISTS unresponsive_seconds  INTEGER;

-- When the device reported update_succeeded (its health window starts), and the verdict.
ALTER TABLE rollout_devices ADD COLUMN IF NOT EXISTS window_started_at TIMESTAMPTZ;
ALTER TABLE rollout_devices ADD COLUMN IF NOT EXISTS verdict           TEXT;
ALTER TABLE rollout_devices ADD COLUMN IF NOT EXISTS verdict_reason    TEXT;
ALTER TABLE rollout_devices ADD COLUMN IF NOT EXISTS verdict_at        TIMESTAMPTZ;

ALTER TABLE rollout_devices DROP CONSTRAINT IF EXISTS rollout_devices_verdict_check;
ALTER TABLE rollout_devices ADD CONSTRAINT rollout_devices_verdict_check
    CHECK (verdict IN ('healthy', 'unhealthy', 'unresponsive', 'failed_update',
                       'no_reply', 'not_reached'));

ALTER TABLE rollout_events DROP CONSTRAINT IF EXISTS rollout_events_event_check;
ALTER TABLE rollout_events ADD CONSTRAINT rollout_events_event_check
    CHECK (event IN ('started', 'stage_advanced', 'halted', 'rollback_started',
                     'rollback_completed', 'completed', 'device_verdict'));

COMMIT;
