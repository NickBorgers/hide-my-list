# Bug 0630: Notion errors lacked safe diagnostics

**Issue:** #630

## Bug Story

Notion API failures raised `httpx.HTTPStatusError` with only the status line and
URL visible to callers. The structured Notion error fields that identify the API
failure class were not logged, so a 4xx response was hard to diagnose once the
failing request shape was no longer in front of the operator.

The diagnostic surface must also follow the repo's private-data logging rule:
Notion response text can echo task content, page titles, reminder text, or other
private values. Logging the raw response body fixes observability by creating a
privacy leak, so the client logs only allowlisted fields and redacts quoted
message content.

## Regression Tests

The tests live in `tests/unit/test_notion_parity.py`:

- `test_notion_http_error_logs_safe_response_metadata` asserts a Notion 4xx logs
  status, code, request id, and a redacted message while preserving
  `httpx.HTTPStatusError`.
- `test_notion_http_error_log_message_is_bounded` asserts the sanitized message
  field has an explicit maximum length.
