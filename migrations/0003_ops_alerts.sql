-- Phase C: ops_alerts table for operational alert delivery.
-- Drains via ops_alerts_drain APScheduler job every 5 min.
-- Throttle per alert_kind managed via ops_alerts_throttle (created in 0001).

BEGIN;

CREATE TABLE IF NOT EXISTS ops_alerts (
  id           UUID PRIMARY KEY,
  alert_kind   TEXT NOT NULL,
  -- alert_kind: short identifier used for throttle lookup, e.g. 'notion_health_failed'
  body         TEXT NOT NULL,
  -- body: human-readable alert text. Must not contain private user data (public repo).
  severity     TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
  state        TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'throttled', 'failed'))
               DEFAULT 'pending',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  delivered_at TIMESTAMPTZ,
  error        TEXT
);

CREATE INDEX IF NOT EXISTS ops_alerts_state_created
  ON ops_alerts (state, created_at);

COMMIT;
