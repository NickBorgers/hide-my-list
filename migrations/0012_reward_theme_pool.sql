-- Per-peer vocabulary of reward image descriptors.
--
-- Promotes the theme / style / palette vocabularies from process constants to
-- stored, per-peer rows so they can grow and retire over time instead of being
-- fixed at deploy. The constants in app/tools/rewards.py remain the seed set
-- and the fallback when this table is unavailable.
--
-- `value` reaches the OpenAI image prompt verbatim. Rows originate from
-- sanitized preference text and (later) from LLM-proposed descriptors, so
-- treat this column as user-influenced text in ops queries: it is filtered by
-- _sanitize_descriptor on the way in, not guaranteed free of personal detail.
--
-- Sensitive-task rewards deliberately do NOT read this table. Their allowlist
-- stays a code constant so that path cannot be steered by stored content.

BEGIN;

CREATE TABLE IF NOT EXISTS reward_theme_pool (
  id           UUID PRIMARY KEY,
  peer         TEXT NOT NULL,
  -- 'theme' | 'style' | 'palette'
  axis         TEXT NOT NULL,
  -- Set for themes ('low'|'medium'|'high'|'epic'); NULL for style and palette,
  -- which share one vocabulary across intensities so their feedback pools.
  intensity    TEXT,
  -- Descriptor text; embedded verbatim into the image prompt.
  value        TEXT NOT NULL,
  -- 'seed'   — copied from the constants in app/tools/rewards.py
  -- 'evolved' — proposed from aggregate feedback
  origin       TEXT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Retirement is a soft delete: retired rows keep their attribution history
  -- and can be resurrected if an axis falls below its minimum size.
  retired_at   TIMESTAMPTZ,
  last_used_at TIMESTAMPTZ,
  use_count    INT NOT NULL DEFAULT 0,
  CONSTRAINT reward_theme_pool_axis_chk
    CHECK (axis IN ('theme', 'style', 'palette')),
  CONSTRAINT reward_theme_pool_origin_chk
    CHECK (origin IN ('seed', 'evolved')),
  -- Themes are scoped to an intensity; style and palette must not be.
  -- The explicit IS NOT NULL is load-bearing: without it, a theme row with a
  -- NULL intensity makes `intensity IN (...)` evaluate to NULL, and a CHECK
  -- that evaluates to NULL is treated as satisfied.
  CONSTRAINT reward_theme_pool_intensity_chk
    CHECK (
      (
        axis = 'theme'
        AND intensity IS NOT NULL
        AND intensity IN ('low', 'medium', 'high', 'epic')
      )
      OR (axis <> 'theme' AND intensity IS NULL)
    )
);

-- Makes seeding idempotent under concurrency: two processes racing to seed the
-- same peer both succeed, with ON CONFLICT DO NOTHING absorbing the loser.
-- COALESCE because NULL intensity would otherwise defeat the uniqueness check
-- for style and palette rows.
CREATE UNIQUE INDEX IF NOT EXISTS reward_theme_pool_uniq
  ON reward_theme_pool (peer, axis, COALESCE(intensity, ''), value);

-- Selection reads only active rows for one peer and axis.
CREATE INDEX IF NOT EXISTS reward_theme_pool_active
  ON reward_theme_pool (peer, axis, intensity)
  WHERE retired_at IS NULL;

COMMENT ON TABLE reward_theme_pool IS
  'Per-peer reward image descriptor vocabulary. value is embedded verbatim into image prompts; treat as user-influenced text.';

COMMIT;
