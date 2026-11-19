-- Staged canary rollout (FR-2): each rollout's stage plan, each device's stage, and the
-- 'skipped' state for devices in later stages that never received the update because the
-- rollout halted first.
-- init.sql already includes these changes, so fresh databases do not need this.
-- Safe to run more than once.
--
--   docker compose exec -T postgres psql -U ota -d ota < registries/postgres/migrations/004_staged_rollout.sql

BEGIN;

ALTER TABLE rollouts ADD COLUMN IF NOT EXISTS stage_count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE rollouts ADD COLUMN IF NOT EXISTS stage_plan  TEXT;     -- as given, e.g. '1,3,6'

ALTER TABLE rollout_devices ADD COLUMN IF NOT EXISTS stage INTEGER NOT NULL DEFAULT 1;

ALTER TABLE rollout_devices DROP CONSTRAINT IF EXISTS rollout_devices_state_check;
ALTER TABLE rollout_devices ADD CONSTRAINT rollout_devices_state_check
    CHECK (state IN ('pending', 'commanded', 'updated', 'failed', 'rolled_back', 'skipped'));

COMMIT;
