-- Phase B reward feedback columns: add feedback_emoji and feedback_at to reward_manifests.
-- feedback_score already exists from 0002_reward_manifests.sql.
-- These columns complete the three-field feedback contract used by record_reward_feedback().

BEGIN;

ALTER TABLE reward_manifests
  ADD COLUMN IF NOT EXISTS feedback_emoji TEXT,
  ADD COLUMN IF NOT EXISTS feedback_at    TIMESTAMPTZ;

-- feedback_emoji: raw emoji character(s) from the Signal reaction, stored verbatim.
-- feedback_at: timestamp when the reaction was recorded; NULL = not yet rated.
-- feedback_at IS NULL is used by record_reward_feedback() for idempotency.

COMMIT;
