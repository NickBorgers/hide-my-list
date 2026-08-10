-- Phase B reward subsystem: reward_manifests table.
-- Stores private reward delivery records. task_title is private — never logged.

BEGIN;

CREATE TABLE IF NOT EXISTS reward_manifests (
  id                   UUID PRIMARY KEY,
  peer                 TEXT NOT NULL,
  notion_page_id       TEXT NOT NULL,
  -- task_title is PRIVATE user data. Never log to stdout, never include in
  -- structured log fields, never commit sample data to the repo.
  task_title           TEXT NOT NULL,
  reward_kind          TEXT NOT NULL,
  -- reward_kind values: 'emoji', 'emoji+image', 'image_fallback'
  intensity            TEXT NOT NULL,
  -- intensity values: 'lightest', 'low', 'medium', 'high', 'epic'
  streak_count         INT NOT NULL DEFAULT 1,
  delivered_at         TIMESTAMPTZ NOT NULL,
  artifact_path        TEXT,
  -- artifact_path: absolute path to generated image on the reward_artifacts volume.
  -- Never written to repo, never committed. NULL when no image generated.
  feedback_score       INT,
  -- feedback_score: -1 (negative), 0 (neutral), 1 (positive). Nullable.
  feedback_note        TEXT,
  -- feedback_note: user-supplied annotation. Private — never log.
  sensitive_task       BOOLEAN NOT NULL DEFAULT false,
  -- sensitive_task: true when task was classified as therapy/medical/legal/financial.
  -- Triggers metaphorical imagery and muted rewards.
  created_at           TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS reward_manifests_peer_delivered
  ON reward_manifests (peer, delivered_at DESC);

CREATE INDEX IF NOT EXISTS reward_manifests_notion_page
  ON reward_manifests (notion_page_id);

COMMIT;
