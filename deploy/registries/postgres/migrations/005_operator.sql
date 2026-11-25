-- Record who made each rollout decision (threat R2): the operator who started or rolled
-- back a rollout, or 'orchestrator' for automatic decisions. Earlier events keep NULL
-- (not recorded at the time).
-- init.sql already includes this change, so fresh databases do not need it.
-- Safe to run more than once.
--
--   docker compose exec -T postgres psql -U ota -d ota < registries/postgres/migrations/005_operator.sql

BEGIN;

ALTER TABLE rollout_events ADD COLUMN IF NOT EXISTS actor TEXT;
ALTER TABLE rollout_events ALTER COLUMN actor SET DEFAULT 'orchestrator';

COMMIT;
