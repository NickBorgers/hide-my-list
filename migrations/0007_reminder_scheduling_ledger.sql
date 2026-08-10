-- Ledger of deadline-driven reminders scheduled by intake (inline) and the
-- nightly backstop daemon. One row per (task, milestone) tuple. Supersession
-- via superseded_at lets the daemon revise the series when the deadline
-- changes in Notion without violating the outbox's idempotency_key UNIQUE.

CREATE TABLE IF NOT EXISTS reminder_scheduling_ledger (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  notion_page_id      TEXT NOT NULL,
  deadline_at         TIMESTAMPTZ NOT NULL,
  urgency             INT NOT NULL,
  tier                TEXT NOT NULL CHECK (tier IN ('dense','standard','sparse')),
  milestone_label     TEXT NOT NULL,
  ideal_slot_at       TIMESTAMPTZ NOT NULL,
  assigned_slot_at    TIMESTAMPTZ NOT NULL,
  reminder_outbox_id  UUID NOT NULL REFERENCES reminder_outbox(id),
  scheduled_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  superseded_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS rsl_assigned_slot
  ON reminder_scheduling_ledger (assigned_slot_at)
  WHERE superseded_at IS NULL;

CREATE INDEX IF NOT EXISTS rsl_page
  ON reminder_scheduling_ledger (notion_page_id)
  WHERE superseded_at IS NULL;

-- One task can have multiple reminders over time (deadline edits → supersession +
-- fresh series). The reminder_outbox.notion_page_id UNIQUE constraint would block
-- this. Drop it; rely on idempotency_key UNIQUE for at-least-once delivery guarantees.
ALTER TABLE reminder_outbox
  DROP CONSTRAINT IF EXISTS reminder_outbox_notion_page_id_key;

-- Restore uniqueness on the outbox via idempotency_key. The original 0001
-- schema had UNIQUE on notion_page_id (just dropped above to allow multiple
-- reminders per task) and NOT NULL on idempotency_key (no UNIQUE). The UNIQUE
-- constraint on idempotency_key is added by this migration (the 0001 schema has
-- it as NOT NULL but not UNIQUE). The deadline daemon's idempotency_key format
-- is "deadline-<page_id>-<milestone>-<deadline_iso>", unique per
-- (task, milestone, deadline) tuple — exactly the dedup granularity we need.
-- Add UNIQUE so retry races can't double-insert the same reminder.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'reminder_outbox_idempotency_key_key'
  ) THEN
    ALTER TABLE reminder_outbox
      ADD CONSTRAINT reminder_outbox_idempotency_key_key UNIQUE (idempotency_key);
  END IF;
END $$;
