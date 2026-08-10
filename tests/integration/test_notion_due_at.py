"""Integration tests for Notion Due At and Reminder Scheduled At plumbing.

Covers create_task due_at_iso payload shape, query_tasks_with_unscheduled_deadlines(),
and mark_reminder_scheduled(). All tests use mocked httpx — no DATABASE_URL required.

Private data discipline: no real page IDs, titles, or phone numbers.
All test values use placeholder strings.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from pytest_httpserver import HTTPServer

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
    base_url = httpserver.url_for("/").rstrip("/")

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


def _captured_body(request_data: bytes) -> dict:  # type: ignore[type-arg]
    return json.loads(request_data.decode("utf-8"))  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# create_task — due_at_iso integration coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_due_at_included_in_payload(
    notion_server: HTTPServer, fake_db_id: str
) -> None:
    """create_task with due_at_iso includes Due At date.start in the POST body."""
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
    assert body["properties"]["Due At"]["date"]["start"] == due_at


@pytest.mark.asyncio
async def test_create_task_due_at_omitted_when_none(
    notion_server: HTTPServer, fake_db_id: str
) -> None:
    """create_task without due_at_iso omits Due At entirely from the POST body."""
    notion_server.expect_request("/pages", method="POST").respond_with_json(
        {"object": "page", "id": "fake-page-id"}
    )

    await notion_module.create_task(
        title="Test task",
        work_type="Independent",
    )

    req = notion_server.log[0][0]
    body = _captured_body(req.data)
    assert "Due At" not in body["properties"]


# ---------------------------------------------------------------------------
# query_tasks_with_unscheduled_deadlines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_unscheduled_deadlines_filter_shape(
    notion_server: HTTPServer, fake_db_id: str
) -> None:
    """query_tasks_with_unscheduled_deadlines sends all 4 AND filter conditions."""
    notion_server.expect_request(
        f"/databases/{fake_db_id}/query", method="POST"
    ).respond_with_json({"results": []})

    await notion_module.query_tasks_with_unscheduled_deadlines()

    req = notion_server.log[0][0]
    body = _captured_body(req.data)
    filters = body["filter"]["and"]

    due_at_f = next(f for f in filters if f.get("property") == "Due At")
    sched_at_f = next(f for f in filters if f.get("property") == "Reminder Scheduled At")
    status_f = next(f for f in filters if f.get("property") == "Status")
    is_rem_f = next(f for f in filters if f.get("property") == "Is Reminder")

    assert due_at_f["date"]["is_not_empty"] is True
    assert sched_at_f["date"]["is_empty"] is True
    assert status_f["select"]["does_not_equal"] == "Completed"
    assert is_rem_f["checkbox"]["equals"] is False


@pytest.mark.asyncio
async def test_query_unscheduled_deadlines_sort_ascending(
    notion_server: HTTPServer, fake_db_id: str
) -> None:
    """query_tasks_with_unscheduled_deadlines sorts by Due At ascending."""
    notion_server.expect_request(
        f"/databases/{fake_db_id}/query", method="POST"
    ).respond_with_json({"results": []})

    await notion_module.query_tasks_with_unscheduled_deadlines()

    req = notion_server.log[0][0]
    body = _captured_body(req.data)
    assert body["sorts"] == [{"property": "Due At", "direction": "ascending"}]


@pytest.mark.asyncio
async def test_query_unscheduled_deadlines_returns_response(
    notion_server: HTTPServer, fake_db_id: str
) -> None:
    """query_tasks_with_unscheduled_deadlines propagates the Notion response."""
    page = {"object": "page", "id": "fake-page-id"}
    notion_server.expect_request(
        f"/databases/{fake_db_id}/query", method="POST"
    ).respond_with_json({"results": [page]})

    result = await notion_module.query_tasks_with_unscheduled_deadlines()

    assert result["results"] == [page]


# ---------------------------------------------------------------------------
# query_scheduled_tasks_with_deadlines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_scheduled_deadlines_filter_shape(
    notion_server: HTTPServer, fake_db_id: str
) -> None:
    """query_scheduled_tasks_with_deadlines sends the edit-detection filter."""
    notion_server.expect_request(
        f"/databases/{fake_db_id}/query", method="POST"
    ).respond_with_json({"results": []})

    await notion_module.query_scheduled_tasks_with_deadlines()

    req = notion_server.log[0][0]
    body = _captured_body(req.data)
    filters = body["filter"]["and"]

    due_at_f = next(f for f in filters if f.get("property") == "Due At")
    sched_at_f = next(f for f in filters if f.get("property") == "Reminder Scheduled At")
    status_f = next(f for f in filters if f.get("property") == "Status")
    is_rem_f = next(f for f in filters if f.get("property") == "Is Reminder")

    assert due_at_f["date"]["is_not_empty"] is True
    assert sched_at_f["date"]["is_not_empty"] is True
    assert status_f["select"]["does_not_equal"] == "Completed"
    assert is_rem_f["checkbox"]["equals"] is False


# ---------------------------------------------------------------------------
# mark_reminder_scheduled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_reminder_scheduled_patch_payload(
    notion_server: HTTPServer, fake_page_id: str
) -> None:
    """mark_reminder_scheduled PATCHes Reminder Scheduled At with a date.start value."""
    notion_server.expect_request(
        f"/pages/{fake_page_id}", method="PATCH"
    ).respond_with_json({"object": "page", "id": fake_page_id})

    await notion_module.mark_reminder_scheduled(fake_page_id)

    req = notion_server.log[0][0]
    body = _captured_body(req.data)
    props = body["properties"]
    assert "Reminder Scheduled At" in props
    assert "start" in props["Reminder Scheduled At"]["date"]


@pytest.mark.asyncio
async def test_mark_reminder_scheduled_utc_timestamp(
    notion_server: HTTPServer, fake_page_id: str
) -> None:
    """mark_reminder_scheduled sets a UTC timestamp close to call time."""
    notion_server.expect_request(
        f"/pages/{fake_page_id}", method="PATCH"
    ).respond_with_json({"object": "page", "id": fake_page_id})

    before = datetime.now(UTC)
    await notion_module.mark_reminder_scheduled(fake_page_id)

    req = notion_server.log[0][0]
    body = _captured_body(req.data)
    ts_str = body["properties"]["Reminder Scheduled At"]["date"]["start"]
    parsed = datetime.fromisoformat(ts_str)
    assert parsed.tzinfo is not None
    assert abs((parsed - before).total_seconds()) <= 2


@pytest.mark.asyncio
async def test_mark_reminder_scheduled_returns_response(
    notion_server: HTTPServer, fake_page_id: str
) -> None:
    """mark_reminder_scheduled propagates the Notion PATCH response."""
    notion_server.expect_request(
        f"/pages/{fake_page_id}", method="PATCH"
    ).respond_with_json({"object": "page", "id": fake_page_id})

    result = await notion_module.mark_reminder_scheduled(fake_page_id)

    assert result["id"] == fake_page_id
