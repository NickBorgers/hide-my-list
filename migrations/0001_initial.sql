-- Phase A initial schema migration.
-- Creates outbox, recent_outbound, and ops_alerts_throttle tables.

BEGIN;

CREATE TABLE IF NOT EXISTS reminder_outbox (
  id                   UUID PRIMARY KEY,
  notion_page_id       TEXT NOT NULL UNIQUE,
  peer                 TEXT NOT NULL,
  body                 TEXT NOT NULL,
  due_at               TIMESTAMPTZ NOT NULL,
  state                TEXT NOT NULL CHECK (state IN ('pending','scheduled','delivering','delivered','failed','dead')),
  attempt              INT NOT NULL DEFAULT 0,
  last_error           TEXT,
  locked_until         TIMESTAMPTZ,
  worker_id            TEXT,
  idempotency_key      TEXT NOT NULL,
  signal_timestamp     BIGINT,
  delivered_at         TIMESTAMPTZ,
  created_at           TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS reminder_outbox_state_due_at
  ON reminder_outbox (state, due_at);

CREATE TABLE IF NOT EXISTS recent_outbound (
  peer                 TEXT NOT NULL,
  signal_timestamp     BIGINT NOT NULL,
  notion_page_id       TEXT NOT NULL,
  -- reminder_type: e.g. 'reminder', 'check_in', 'task_complete'
  -- required for context-free reply classification (avoids asking user to clarify)
  reminder_type        TEXT NOT NULL DEFAULT 'reminder',
  -- title: the reminder/task title, enables "I did it" -> COMPLETE without re-asking
  title                TEXT NOT NULL DEFAULT '',
  -- prompt_kind: e.g. 'sent', 'missed' — status context for follow-up replies
  prompt_kind          TEXT NOT NULL DEFAULT 'sent',
  sent_at              TIMESTAMPTZ NOT NULL,
  awaiting_reply       BOOLEAN NOT NULL DEFAULT true,
  -- expires_at uses 24h for reminders (reduces stale-context misclassification)
  expires_at           TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (peer, signal_timestamp)
);

CREATE TABLE IF NOT EXISTS ops_alerts_throttle (
  alert_kind           TEXT PRIMARY KEY,
  last_sent_at         TIMESTAMPTZ NOT NULL
);

COMMIT;
