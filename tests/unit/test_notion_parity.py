"""Parity tests for app/tools/notion.py vs scripts/notion-cli.sh.

Each test verifies that the Python client makes the same HTTP request
(URL, method, relevant headers, JSON body) that the bash curl command
in notion-cli.sh would make.

Uses pytest-httpserver to capture actual HTTP traffic from the Python client.

All 9 verbs are tested:
  1. create_task
  2. create_reminder
  3. query_pending
  4. query_all
  5. query_due_reminders
  6. update_status
  7. complete_reminder
  8. update_property
  9. get_page

Private data discipline: no real page IDs, titles, or phone numbers.
All test values use placeholder strings.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from pytest_httpserver import HTTPServer
from structlog.testing import capture_logs

import app.tools.notion as notion_module

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_page_id() -> str:
    return str(uuid.uuid4()).replace("-", "")


@pytest.fixture()
def fake_db_id() -> str:
    return str(uuid.uuid4()).replace("-", "")


@pytest.fixture()
def notion_server(
    httpserver: HTTPServer, fake_db_id: str, monkeypatch: pytest.MonkeyPatch
) -> HTTPServer:
    """Configure environment and redirect httpx to the test server."""
    base_url = httpserver.url_for("/")
    # Strip trailing slash for compatibility with httpx base_url
    base_url = base_url.rstrip("/")

    monkeypatch.setenv("NOTION_API_KEY", "test-api-key")
    monkeypatch.setenv("NOTION_DATABASE_ID", fake_db_id)

    import httpx

    def _test_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": "Bearer test-api-key",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0),
        )

    monkeypatch.setattr(notion_module, "_client_factory", _test_client)
    return httpserver


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _captured_body(request_data: bytes) -> dict:  # type: ignore[type-arg]
    """Parse JSON body from captured request bytes."""
    return json.loads(request_data.decode("utf-8"))  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Verb 1 — create_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_request(notion_server: HTTPServer, fake_db_id: str) -> None:
    """create_task sends POST /pages with correct properties body."""
    notion_server.expect_request("/pages", method="POST").respond_with_json(
        {"object": "page", "id": "fake-page-id"}
    )

    await notion_module.create_task(
        title="Test task",
        work_type="Independent",
        urgency=75,
        time_estimate=20,
        energy_required="Low",
        inline_steps="Step 1\nStep 2",
        status="Pending",
    )

    assert len(notion_server.log) == 1
    req = notion_server.log[0][0]
    body = _captured_body(req.data)

    assert req.method == "POST"
    assert req.path == "/pages"
    assert body["parent"]["database_id"] == fake_db_id

    props = body["properties"]
    assert props["Title"]["title"][0]["text"]["content"] == "Test task"
    assert props["Status"]["select"]["name"] == "Pending"
    assert props["Work Type"]["select"]["name"] == "Independent"
    assert props["Urgency"]["number"] == 75
    assert props["Time Estimate (min)"]["number"] == 20
    assert props["Energy Required"]["select"]["name"] == "Low"
    assert props["Rejection Count"]["number"] == 0
    assert props["Steps Completed"]["number"] == 0
    assert props["Resume Count"]["number"] == 0
    assert props["Inline Steps"]["rich_text"][0]["text"]["content"] == "Step 1\nStep 2"


@pytest.mark.asyncio
async def test_create_task_with_due_at(notion_server: HTTPServer, fake_db_id: str) -> None:
    """create_task with due_at_iso includes Due At date property in payload."""
    notion_server.expect_request("/pages", method="POST").respond_with_json(
        {"object": "page", "id": "fake-page-id"}
    )

    due_at = "2026-06-03T17:00:00-05:00"
    await notion_module.create_task(
        title="Test task",
        work_type="Independent",
        due_at_iso=due_at,
    )

    req = notion_server.log[0][0]
    body = _captured_body(req.data)
    props = body["properties"]
    assert "Due At" in props
    assert props["Due At"]["date"]["start"] == due_at


@pytest.mark.asyncio
async def test_create_task_without_due_at(notion_server: HTTPServer, fake_db_id: str) -> None:
    """create_task with due_at_iso=None (default) omits Due At from the payload."""
    notion_server.expect_request("/pages", method="POST").respond_with_json(
        {"object": "page", "id": "fake-page-id"}
    )

    await notion_module.create_task(
        title="Test task",
        work_type="Independent",
        # due_at_iso omitted — defaults to None
    )

    req = notion_server.log[0][0]
    body = _captured_body(req.data)
    props = body["properties"]
    assert "Due At" not in props


@pytest.mark.asyncio
async def test_create_task_with_parent(
    notion_server: HTTPServer, fake_db_id: str, fake_page_id: str
) -> None:
    """create_task includes Parent Task relation when parent_id is set."""
    notion_server.expect_request("/pages", method="POST").respond_with_json(
        {"object": "page", "id": "fake-page-id"}
    )

    await notion_module.create_task(
        title="Sub task",
        work_type="Creative",
        parent_id=fake_page_id,
        sequence=2,
    )

    req = notion_server.log[0][0]
    body = _captured_body(req.data)
    props = body["properties"]
    assert props["Parent Task"]["relation"][0]["id"] == fake_page_id
    assert props["Sequence"]["number"] == 2


# ---------------------------------------------------------------------------
# Verb 2 — create_reminder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_reminder_request(notion_server: HTTPServer, fake_db_id: str) -> None:
    """create_reminder sends POST /pages with Is Reminder=true and Remind At date."""
    notion_server.expect_request("/pages", method="POST").respond_with_json(
        {"object": "page", "id": "fake-reminder-id"}
    )

    remind_at = "2026-06-01T18:00:00-05:00"
    await notion_module.create_reminder(
        title="Test reminder",
        remind_at_iso=remind_at,
        work_type="Social",
        energy_required="Low",
    )

    req = notion_server.log[0][0]
    body = _captured_body(req.data)
    assert body["parent"]["database_id"] == fake_db_id

    props = body["properties"]
    assert props["Is Reminder"]["checkbox"] is True
    assert props["Remind At"]["date"]["start"] == remind_at
    assert props["Reminder Status"]["select"]["name"] == "pending"
    assert props["Status"]["select"]["name"] == "Pending"
    assert props["Urgency"]["number"] == 90
    assert props["Time Estimate (min)"]["number"] == 5


# ---------------------------------------------------------------------------
# Verb 3 — query_pending
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_pending_request(notion_server: HTTPServer, fake_db_id: str) -> None:
    """query_pending sends POST /databases/{db_id}/query with correct filter."""
    notion_server.expect_request(f"/databases/{fake_db_id}/query", method="POST").respond_with_json(
        {"results": []}
    )

    await notion_module.query_pending()

    req = notion_server.log[0][0]
    body = _captured_body(req.data)
    filters = body["filter"]["and"]

    # Must filter Status=Pending and Is Reminder=false
    status_filter = next(f for f in filters if "Status" in f.get("property", ""))
    reminder_filter = next(f for f in filters if "Is Reminder" in f.get("property", ""))

    assert status_filter["select"]["equals"] == "Pending"
    assert reminder_filter["checkbox"]["equals"] is False
    assert body["sorts"][0]["direction"] == "descending"


# ---------------------------------------------------------------------------
# Verb 4 — query_all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_all_request(notion_server: HTTPServer, fake_db_id: str) -> None:
    """query_all sends POST /databases/{db_id}/query with urgency sort only."""
    notion_server.expect_request(f"/databases/{fake_db_id}/query", method="POST").respond_with_json(
        {"results": []}
    )

    await notion_module.query_all()

    req = notion_server.log[0][0]
    body = _captured_body(req.data)
    # No filter key
    assert "filter" not in body
    assert body["sorts"][0]["property"] == "Urgency"
    assert body["sorts"][0]["direction"] == "descending"


# ---------------------------------------------------------------------------
# Verb 5 — query_due_reminders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_due_reminders_request(notion_server: HTTPServer, fake_db_id: str) -> None:
    """query_due_reminders sends POST with Is Reminder=true, status=pending, on_or_before."""
    notion_server.expect_request(f"/databases/{fake_db_id}/query", method="POST").respond_with_json(
        {"results": []}
    )

    before_iso = "2026-06-01T20:00:00+00:00"
    await notion_module.query_due_reminders(before_iso=before_iso)

    req = notion_server.log[0][0]
    body = _captured_body(req.data)
    filters = body["filter"]["and"]

    is_reminder = next(f for f in filters if f.get("property") == "Is Reminder")
    rem_status = next(f for f in filters if f.get("property") == "Reminder Status")
    remind_at = next(f for f in filters if f.get("property") == "Remind At")

    assert is_reminder["checkbox"]["equals"] is True
    assert rem_status["select"]["equals"] == "pending"
    assert remind_at["date"]["on_or_before"] == before_iso
    assert body["sorts"][0]["direction"] == "ascending"


@pytest.mark.asyncio
async def test_query_due_reminders_default_time(notion_server: HTTPServer, fake_db_id: str) -> None:
    """query_due_reminders without before_iso uses current UTC time."""
    notion_server.expect_request(f"/databases/{fake_db_id}/query", method="POST").respond_with_json(
        {"results": []}
    )

    before = datetime.now(UTC)
    await notion_module.query_due_reminders()

    req = notion_server.log[0][0]
    body = _captured_body(req.data)
    filters = body["filter"]["and"]
    remind_at = next(f for f in filters if f.get("property") == "Remind At")
    ts_str = remind_at["date"]["on_or_before"]

    # The timestamp in the body should be close to the current UTC time.
    # We allow 2 seconds of slack to avoid microsecond precision issues
    # (notion-cli.sh uses date -u which is also second-precision).
    parsed = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    assert abs((parsed - before).total_seconds()) <= 2


# ---------------------------------------------------------------------------
# Verb 6 — update_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_status_completed(notion_server: HTTPServer, fake_page_id: str) -> None:
    """update_status to Completed sends GET then PATCH with Completed At."""
    # First request: GET the page (to check Started At)
    notion_server.expect_request(f"/pages/{fake_page_id}", method="GET").respond_with_json(
        {"object": "page", "id": fake_page_id, "properties": {}}
    )
    # Second request: PATCH
    notion_server.expect_request(f"/pages/{fake_page_id}", method="PATCH").respond_with_json(
        {"object": "page", "id": fake_page_id}
    )

    await notion_module.update_status(fake_page_id, "Completed")

    assert len(notion_server.log) == 2
    patch_req = notion_server.log[1][0]
    body = _captured_body(patch_req.data)
    props = body["properties"]
    assert props["Status"]["select"]["name"] == "Completed"
    assert "Completed At" in props


@pytest.mark.asyncio
async def test_update_status_in_progress_sets_started_at(
    notion_server: HTTPServer, fake_page_id: str
) -> None:
    """update_status to In Progress sets Started At when not already set."""
    notion_server.expect_request(f"/pages/{fake_page_id}", method="GET").respond_with_json(
        {"object": "page", "id": fake_page_id, "properties": {"Started At": {"date": None}}}
    )
    notion_server.expect_request(f"/pages/{fake_page_id}", method="PATCH").respond_with_json(
        {"object": "page", "id": fake_page_id}
    )

    await notion_module.update_status(fake_page_id, "In Progress")

    patch_req = notion_server.log[1][0]
    body = _captured_body(patch_req.data)
    props = body["properties"]
    assert props["Status"]["select"]["name"] == "In Progress"
    assert "Started At" in props


@pytest.mark.asyncio
async def test_update_status_in_progress_no_overwrite(
    notion_server: HTTPServer, fake_page_id: str
) -> None:
    """update_status to In Progress does NOT overwrite an existing Started At."""
    notion_server.expect_request(f"/pages/{fake_page_id}", method="GET").respond_with_json(
        {
            "object": "page",
            "id": fake_page_id,
            "properties": {"Started At": {"date": {"start": "2026-01-01T10:00:00Z"}}},
        }
    )
    notion_server.expect_request(f"/pages/{fake_page_id}", method="PATCH").respond_with_json(
        {"object": "page", "id": fake_page_id}
    )

    await notion_module.update_status(fake_page_id, "In Progress")

    patch_req = notion_server.log[1][0]
    body = _captured_body(patch_req.data)
    props = body["properties"]
    # Started At should NOT appear — it's already set
    assert "Started At" not in props


# ---------------------------------------------------------------------------
# Verb 7 — complete_reminder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_reminder_sent(notion_server: HTTPServer, fake_page_id: str) -> None:
    """complete_reminder sends PATCH with Status=Completed, Reminder Status=sent."""
    notion_server.expect_request(f"/pages/{fake_page_id}", method="PATCH").respond_with_json(
        {"object": "page", "id": fake_page_id}
    )

    await notion_module.complete_reminder(fake_page_id, "sent")

    req = notion_server.log[0][0]
    body = _captured_body(req.data)
    props = body["properties"]
    assert props["Status"]["select"]["name"] == "Completed"
    assert props["Reminder Status"]["select"]["name"] == "sent"
    assert "Completed At" in props


@pytest.mark.asyncio
async def test_complete_reminder_invalid_status(fake_page_id: str) -> None:
    """complete_reminder raises ValueError for unknown reminder_status values."""
    with pytest.raises(ValueError, match="reminder_status"):
        await notion_module.complete_reminder(fake_page_id, "invalid")


# ---------------------------------------------------------------------------
# Verb 8 — update_property
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_property_request(notion_server: HTTPServer, fake_page_id: str) -> None:
    """update_property sends PATCH with arbitrary JSON body."""
    notion_server.expect_request(f"/pages/{fake_page_id}", method="PATCH").respond_with_json(
        {"object": "page", "id": fake_page_id}
    )

    prop_json = {"properties": {"Urgency": {"number": 100}}}
    await notion_module.update_property(fake_page_id, prop_json)

    req = notion_server.log[0][0]
    body = _captured_body(req.data)
    assert body["properties"]["Urgency"]["number"] == 100


# ---------------------------------------------------------------------------
# Verb 9 — get_page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_page_request(notion_server: HTTPServer, fake_page_id: str) -> None:
    """get_page sends GET /pages/{page_id}."""
    notion_server.expect_request(f"/pages/{fake_page_id}", method="GET").respond_with_json(
        {"object": "page", "id": fake_page_id}
    )

    result = await notion_module.get_page(fake_page_id)

    assert len(notion_server.log) == 1
    req = notion_server.log[0][0]
    assert req.method == "GET"
    assert req.path == f"/pages/{fake_page_id}"
    assert result["id"] == fake_page_id


# ---------------------------------------------------------------------------
# Authorization header check (spot-check one verb)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notion_sends_authorization_header(
    notion_server: HTTPServer, fake_db_id: str
) -> None:
    """Verify Authorization and Notion-Version headers are set."""
    notion_server.expect_request(f"/databases/{fake_db_id}/query", method="POST").respond_with_json(
        {"results": []}
    )

    await notion_module.query_all()

    req = notion_server.log[0][0]
    assert req.headers.get("authorization") == "Bearer test-api-key"
    assert req.headers.get("notion-version") == "2022-06-28"


@pytest.mark.asyncio
async def test_notion_http_error_logs_safe_response_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Notion 4xx logs safe diagnostics while preserving HTTPStatusError."""
    monkeypatch.setenv("NOTION_DATABASE_ID", "test-database-id")
    error_body = {
        "object": "error",
        "status": 400,
        "code": "validation_error",
        "message": 'Property "Private task title" does not exist.',
    }

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=400,
            json=error_body,
            headers={"x-request-id": "notion-request-id"},
        )

    def _test_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://api.notion.com/v1",
            transport=httpx.MockTransport(_handler),
        )

    monkeypatch.setattr(notion_module, "_client_factory", _test_client)

    with capture_logs() as captured, pytest.raises(httpx.HTTPStatusError):
        await notion_module.query_pending()

    [error_log] = [event for event in captured if event["event"] == "notion.http_error"]
    assert error_log["status_code"] == 400
    assert error_log["notion_error_code"] == "validation_error"
    assert error_log["notion_error_status"] == 400
    assert error_log["notion_request_id"] == "notion-request-id"
    assert "response_body" not in error_log
    assert "Private task title" not in str(error_log)
    assert '"<redacted>"' in error_log["notion_error_message"]


@pytest.mark.asyncio
async def test_notion_http_error_log_message_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sanitized Notion message log field has an explicit maximum length."""
    long_message = "x" * (notion_module._NOTION_ERROR_MESSAGE_MAX_CHARS + 50)

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=500,
            json={
                "object": "error",
                "status": 500,
                "code": "internal_server_error",
                "message": long_message,
            },
        )

    def _test_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://api.notion.com/v1",
            transport=httpx.MockTransport(_handler),
        )

    monkeypatch.setattr(notion_module, "_client_factory", _test_client)

    with capture_logs() as captured, pytest.raises(httpx.HTTPStatusError):
        await notion_module.get_page("<page-id>")

    [error_log] = [event for event in captured if event["event"] == "notion.http_error"]
    assert len(error_log["notion_error_message"]) == notion_module._NOTION_ERROR_MESSAGE_MAX_CHARS
