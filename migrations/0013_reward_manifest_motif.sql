-- Reward image relevance + fallback diagnosability.
--
-- motif records which task motif drove the image prompt. Before this column,
-- nothing persisted linked a generated image back to the kind of task that
-- earned it, so "is the image relevant?" could only be answered by eyeballing
-- the PNG against task_title — and the PNGs live in a Docker volume with no
-- host bind mount. The motif is a label from the fixed vocabulary in
-- app/tools/rewards.py (_MOTIFS): generic English, no task text. NULL for
-- emoji-only and sensitive rewards, for classification failures, and for every
-- row written before this migration.
--
-- image_failure_reason records why a delivery fell back to text instead of an
-- image ('no_api_key', 'not_eligible', 'api_error', 'empty_response'). The
-- failure was previously visible only in a log line with no reason field, which
-- made a fallback unexplainable once logs aged out of retention. NULL when an
-- image was generated, when none was attempted, and on pre-migration rows.
-- Both values are fixed vocabulary — unlike style/palette, they are never
-- user-authored text.

BEGIN;

ALTER TABLE reward_manifests
  ADD COLUMN IF NOT EXISTS motif                TEXT,
  ADD COLUMN IF NOT EXISTS image_failure_reason TEXT;

COMMIT;
