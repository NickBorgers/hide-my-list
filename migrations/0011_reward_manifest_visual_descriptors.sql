-- Reward image feedback attribution: record which visual choices produced each
-- reward image, so a later emoji reaction can be attributed back to them.
--
-- Without these columns, apply_feedback_weight() has nothing to correlate a
-- reaction against: load_feedback_history() could return the score but not the
-- theme/style/palette that earned it, so per-theme learning was impossible.
--
-- These columns originate from user_prefs.rewards preference strings, which are
-- intended as art descriptors but are not validated or allowlisted at write time.
-- Treat style/palette as user-provided text; do not assume they are free of
-- personal detail in ops queries. They are NULL for emoji-only rewards, for rows
-- written when image generation fails, and for every row written before this
-- migration; load_feedback_history() coerces NULL to '' so historical rows
-- simply never match a candidate.

BEGIN;

ALTER TABLE reward_manifests
  ADD COLUMN IF NOT EXISTS theme_family TEXT,
  ADD COLUMN IF NOT EXISTS style        TEXT,
  ADD COLUMN IF NOT EXISTS palette      TEXT;

-- Feedback lookups scan rated rows for one peer within a 90-day window; this
-- index keeps that scan off a full table sweep as the manifest table grows.
CREATE INDEX IF NOT EXISTS reward_manifests_peer_feedback
  ON reward_manifests (peer, feedback_at DESC)
  WHERE feedback_at IS NOT NULL;

COMMIT;
