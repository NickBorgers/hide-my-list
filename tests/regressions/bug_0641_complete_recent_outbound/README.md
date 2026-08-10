# Bug #641: COMPLETE must prefer the unresolved reminder context

A terse COMPLETE reply can arrive after a reminder worker inserts `recent_outbound`.
A stale checkpointed `active_task` must not silently win over that unresolved reminder.
The regression test asserts that the reminder page is rewarded and cleared while the stale active task is not patched in Notion.
