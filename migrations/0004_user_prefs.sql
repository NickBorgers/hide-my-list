-- Stores per-peer user preferences as JSON.
-- Keyed by peer (E.164 Signal number — the user identifier).

BEGIN;

CREATE TABLE IF NOT EXISTS user_prefs (
  peer                   TEXT PRIMARY KEY,
  -- raw JSON blob of all user preferences.
  -- Using jsonb for flexible schema as preferences evolve.
  prefs_json             JSONB NOT NULL DEFAULT '{}',
  -- reward_prefs: subset of prefs_json promoted for fast access.
  reward_intensity       TEXT NOT NULL DEFAULT 'medium',
  -- reward_intensity: 'lightest', 'low', 'medium', 'high', 'epic'
  reward_kinds_enabled   JSONB NOT NULL DEFAULT '["emoji","image"]',
  -- reward_kinds_enabled: JSON array of enabled reward kinds
  sensitive_task_mode    BOOLEAN NOT NULL DEFAULT false,
  -- sensitive_task_mode: muted rewards for therapy/medical/financial tasks
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE user_prefs IS
  'Per-peer user preferences. Private — never log prefs_json contents.';

COMMIT;
