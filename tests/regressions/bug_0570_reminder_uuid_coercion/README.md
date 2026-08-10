# Bug 0570: Reminder Worker UUID Coercion

**Issue:** #570
**Fix PR:** #571

## Bug Story

`reminder_worker.py:140` called `uuid.UUID(row["id"])` to convert the row's id
field. psycopg3 returns UUID columns as native `uuid.UUID` objects, not as
strings. Wrapping an already-native `uuid.UUID` in `uuid.UUID()` raises
`AttributeError: 'UUID' object has no attribute 'int'` on some psycopg3 builds,
silently dropping every reminder since the cutover to the Python stack.

The fix added `_coerce_uuid()` which accepts both `uuid.UUID` and string-like
values, making the worker resilient to psycopg3 row type semantics.

## Regression Test

`test_uuid_round_trip.py` — integration test requiring a real Postgres connection
(`DATABASE_URL` env var). Skipped automatically when `DATABASE_URL` is unset.

The test inserts a real reminder row via `app/tools/reminders.py:enqueue` (so the
id field has the native `uuid.UUID` type psycopg3 returns), sets `due_at` to now,
calls `dispatch_due_reminders`, and asserts:
- No `AttributeError` is raised.
- The mock signal send function is awaited exactly once.
- The mock is called with `recipient="<test-peer>"`.
- The outbox row transitions to `state='delivered'`.
