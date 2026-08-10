# Bug 0631: Notion health check blind to database schema

**Issue:** #631 (root cause tracked in #629)

## Bug Story

The nightly deadline scheduler failed with an opaque Notion `400 Bad Request`:

```
reminder_scheduler.notion_query_failed
  app/scheduler/reminder_scheduler.py:28  run_reminder_scheduler
  app/tools/notion.py:361  query_tasks_with_unscheduled_deadlines
httpx.HTTPStatusError: Client error '400 Bad Request' for url
  https://api.notion.com/v1/databases/<db_id>/query
```

The task database was missing the `Due At` and `Reminder Scheduled At`
properties, so Notion rejected any filter naming them.

Throughout, `notion.health_check.ok` reported 200 every 15 minutes. The probe
hits `GET /v1/users/me`, which proves the token is valid and the API is
reachable and proves nothing about the database being queried. The mismatch was
therefore invisible until a verb that touched the missing properties ran — for
`run_reminder_scheduler_job`, a once-daily cron, meaning up to 24 hours between
deploying code that depends on a property and learning the property is absent.

The failure mode is general, not specific to the deadline properties: the client
filters on named properties in several verbs, and nothing verified those names
existed.

## Fix

`app/tools/notion.py` declares `REQUIRED_PROPERTIES` — every property the client
writes or filters on, with its Notion type — and `verify_schema()` compares it
against `GET /v1/databases/{id}`, reporting missing and mistyped properties by
name. `check_notion_health()` runs the schema probe after a successful
connectivity probe and enqueues a distinct `notion_schema_mismatch` ops alert
naming the offending properties.

Detection window for schema drift drops from "whenever the slowest job that
touches the property next runs" to 15 minutes.

## Regression Tests

Tests live in `tests/integration/test_notion_schema_verification.py`:

- `test_missing_property_is_named` replays this exact bug — a database without
  `Due At` and `Reminder Scheduled At` — and asserts both names reach the result
  rather than a bare failure flag.
- `test_wrong_type_is_named_with_both_types` covers the adjacent case where a
  property exists with the wrong type, which 400s identically.
- `test_unreadable_schema_reports_detail_not_missing` keeps "could not read the
  schema" distinguishable from "the schema is wrong".

Job wiring is covered in `tests/integration/test_ops_replacements.py`:

- `test_notion_schema_mismatch_enqueues_alert_naming_properties` asserts the
  property names reach the alert body, since that is the only copy the operator
  sees over Signal.
- `test_connectivity_failure_skips_schema_probe` keeps an outage from producing
  two alerts for one root cause.

The declaration is kept honest by the structural lint
`tests/unit/test_notion_required_properties.py`, which AST-scans
`app/tools/notion.py` for literal property references and fails if any is absent
from `REQUIRED_PROPERTIES`. Without it, a new verb filtering on a new property
would silently reopen the blind spot. The lint found one omission
(`Started At`) on its first run against a hand-written declaration.
