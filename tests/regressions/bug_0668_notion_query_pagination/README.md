# Bug #668: Notion `/query` calls never paginated — task matching went blind past 100 rows

`notion.query_all()` sends no filter at all and sorts by Urgency descending, relying on Notion's
default `page_size` of 100 to return everything in one response. Urgency is static — it never
auto-increases (`docs/notion-schema.md`) — and completed tasks are never archived out of the
database. Once total row count (every status, all-time) passed 100, any task sitting at or below
the 100th-ranked Urgency fell off page 1 and became permanently invisible to matching: a user
reported finishing a task that genuinely was Pending in Notion, and `complete_node`'s title match
came back with `candidate_count: 0` even after widening to the whole open list, so it asked
"which task did you mean?" instead of completing it.

`query_all()`'s Python caller (`app/graph/nodes/complete.py`'s title match, and
`app/graph/nodes/_task_match.py`'s `open_non_reminder_tasks` filter it feeds into) had no bug of
its own — the candidate the message named was simply never in the list it was scoring against.
The same unpaginated `client.post(.../query) → resp.json()` pattern existed identically in four
other verbs: `query_pending()` (feeds `GET_TASK` selection and `REJECT`'s alt-task lookup),
`query_due_reminders()`, `query_tasks_with_unscheduled_deadlines()`, and
`query_scheduled_tasks_with_deadlines()` (both feed the deadline reminder backstop) — all five
carried the same latent truncation risk as task volume grew.

The fix routes all five through one paginating helper, `notion._query_database()`, which follows
`has_more` / `next_cursor` to exhaustion instead of returning after the first page, capped at
`_MAX_QUERY_PAGES` (50 pages / 5,000 rows) as a runaway-loop backstop.

## Coverage

`test_notion_query_pagination.py` proves `_query_database` follows a multi-page `has_more`
sequence and merges `results`, that page 2's request carries page 1's `next_cursor` as
`start_cursor`, that a single-page response is unaffected, and that the page cap stops an
endless `has_more: true` sequence rather than looping forever. It also proves each of the five
query verbs — not just `query_all` — actually route through the paginating helper, since the bug
class was systemic across all five, not specific to completion matching.
