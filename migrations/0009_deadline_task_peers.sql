-- Private routing metadata for deadline reminder backstop jobs.
--
-- Notion task pages do not carry a canonical Signal recipient property. Intake
-- records the peer here before scheduling deadline reminders so nightly orphan
-- catch-up and deadline-edit detection can route reminders without guessing.

CREATE TABLE IF NOT EXISTS deadline_task_peers (
  notion_page_id TEXT PRIMARY KEY,
  peer           TEXT NOT NULL,
  recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
