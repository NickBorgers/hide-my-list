-- Discriminator for wall-clock reminders vs deadline-series milestones.
--
-- The reminder worker completes Notion reminder pages after delivery. Deadline
-- rows point at the user's task page, so they must not use that completion
-- path.

ALTER TABLE reminder_outbox
  ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'reminder';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'reminder_outbox_kind_check'
  ) THEN
    ALTER TABLE reminder_outbox
      ADD CONSTRAINT reminder_outbox_kind_check
      CHECK (kind IN ('reminder', 'deadline'));
  END IF;
END $$;
