# Bug #641: COMPLETE must prefer the unresolved reminder context

A terse COMPLETE reply can arrive after a reminder worker inserts `recent_outbound`.
A stale checkpointed `active_task` must not silently win over that unresolved reminder.
The regression test asserts that the reminder page is rewarded and cleared while the stale active task is not patched in Notion.

## Coverage

`test_complete_recent_outbound.py` covers the node in isolation: it patches
`_load_recent_outbound_target` and asserts the arbitration, the 24h TTL, and the
side effects each source produces.

That leaves the seam itself uncovered. Patching the loader means the test never
exercises the write that feeds it, so it would have passed against the original
bug — `reminder_worker` writing `recent_outbound` and `complete_node` reading it
several turns later is the whole failure, and no single-node test can observe it.

The end-to-end coverage lives in `tests/e2e/scenarios/test_reminder_then_complete.py`,
which delivers the reminder through the real worker and replies through the real
graph:

- `test_done_after_a_reminder_resolves_that_reminder` — the core chain. Fails
  with the `recent_outbound` INSERT in `app/scheduler/reminder_worker.py`
  disabled.
- `test_reminder_wins_over_a_stale_active_task` — the wrong-page half. Fails with
  the 24h TTL check in `app/graph/nodes/complete.py` disabled.
- `test_the_more_recent_context_wins` — a *fresh* `active_task` competing with a
  newer reminder. This is the only one that reaches the arbitration itself: with
  a stale task the TTL disqualifies the checkpoint entry first, so a regression
  to checkpoint-first ordering slips past the other two. Fails with
  `_choose_completion_target` reverted to preferring `active_target`.

`tests/e2e/scenarios/test_cold_complete.py` covers the inverse — "done" with
nothing to attribute it to must ask rather than guess.
