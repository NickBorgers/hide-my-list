"""Regression coverage for bug #668: unpaginated Notion `/query` calls.

`notion.query_all()` and four sibling verbs fetched only Notion's first page (default
`page_size` 100) and returned. Once a database's total row count passed 100, a low-urgency
open task — Urgency is static, per docs/notion-schema.md — could fall off page 1 and become
permanently invisible to `complete_node`'s title match and `intake`'s duplicate check, even
though the task was genuinely open in Notion. All five query verbs shared the same
unpaginated pattern, so the fix and its coverage are both scoped to the shared helper rather
than to `query_all` alone.

Private data discipline: no real page IDs, titles, or phone numbers. All test values use
placeholder strings.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import structlog.testing
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

import app.tools.notion as notion_module

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


def _captured_body(request_data: bytes) -> dict[str, Any]:
    return json.loads(request_data.decode("utf-8"))  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# _query_database — pagination mechanics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paginates_across_has_more_pages(
    notion_server: HTTPServer, fake_db_id: str
) -> None:
    """A has_more:true page followed by has_more:false merges both pages' results."""
    page_one = {"id": "page-one"}
    page_two = {"id": "page-two"}
    notion_server.expect_oneshot_request(
        f"/databases/{fake_db_id}/query", method="POST"
    ).respond_with_json({"results": [page_one], "has_more": True, "next_cursor": "cursor-1"})
    notion_server.expect_oneshot_request(
        f"/databases/{fake_db_id}/query", method="POST"
    ).respond_with_json({"results": [page_two], "has_more": False, "next_cursor": None})

    result = await notion_module._query_database({"sorts": []})

    assert result["results"] == [page_one, page_two]
    assert result["has_more"] is False
    assert result["next_cursor"] is None
    assert len(notion_server.log) == 2


@pytest.mark.asyncio
async def test_second_page_request_carries_first_pages_cursor(
    notion_server: HTTPServer, fake_db_id: str
) -> None:
    """The second request's body sends the first page's next_cursor as start_cursor."""
    notion_server.expect_oneshot_request(
        f"/databases/{fake_db_id}/query", method="POST"
    ).respond_with_json({"results": [], "has_more": True, "next_cursor": "cursor-abc"})
    notion_server.expect_oneshot_request(
        f"/databases/{fake_db_id}/query", method="POST"
    ).respond_with_json({"results": [], "has_more": False, "next_cursor": None})

    await notion_module._query_database({"sorts": []})

    first_body = _captured_body(notion_server.log[0][0].data)
    second_body = _captured_body(notion_server.log[1][0].data)
    assert "start_cursor" not in first_body
    assert second_body["start_cursor"] == "cursor-abc"


@pytest.mark.asyncio
async def test_single_page_response_unaffected(
    notion_server: HTTPServer, fake_db_id: str
) -> None:
    """A response with no has_more key (the common case) returns after one request."""
    page = {"id": "only-page"}
    notion_server.expect_request(
        f"/databases/{fake_db_id}/query", method="POST"
    ).respond_with_json({"results": [page]})

    result = await notion_module._query_database({"sorts": []})

    assert result["results"] == [page]
    assert len(notion_server.log) == 1


@pytest.mark.asyncio
async def test_pagination_capped_at_max_pages(
    notion_server: HTTPServer, fake_db_id: str
) -> None:
    """An endlessly has_more:true sequence stops at _MAX_QUERY_PAGES and logs a warning."""

    def _always_has_more(request: Request) -> Response:
        return Response(
            json.dumps({"results": [{"id": "row"}], "has_more": True, "next_cursor": "same"}),
            mimetype="application/json",
        )

    notion_server.expect_request(
        f"/databases/{fake_db_id}/query", method="POST"
    ).respond_with_handler(_always_has_more)

    with structlog.testing.capture_logs() as logs:
        result = await notion_module._query_database({"sorts": []})

    assert len(result["results"]) == notion_module._MAX_QUERY_PAGES
    assert result["has_more"] is False
    assert len(notion_server.log) == notion_module._MAX_QUERY_PAGES
    capped_events = [e for e in logs if e.get("event") == "notion.query.pagination_capped"]
    assert len(capped_events) == 1, "expected exactly one notion.query.pagination_capped log"


# ---------------------------------------------------------------------------
# All five query verbs route through _query_database, not just query_all
# ---------------------------------------------------------------------------

_VERBS: list[Callable[[], Awaitable[dict[str, Any]]]] = [
    notion_module.query_pending,
    notion_module.query_all,
    notion_module.query_due_reminders,
    notion_module.query_tasks_with_unscheduled_deadlines,
    notion_module.query_scheduled_tasks_with_deadlines,
]


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", _VERBS, ids=lambda f: f.__name__)
async def test_each_query_verb_paginates(
    notion_server: HTTPServer,
    fake_db_id: str,
    verb: Callable[[], Awaitable[dict[str, Any]]],
) -> None:
    """Every query verb — not just query_all — follows has_more rather than truncating."""
    page_one = {"id": "verb-page-one"}
    page_two = {"id": "verb-page-two"}
    notion_server.expect_oneshot_request(
        f"/databases/{fake_db_id}/query", method="POST"
    ).respond_with_json({"results": [page_one], "has_more": True, "next_cursor": "cursor-1"})
    notion_server.expect_oneshot_request(
        f"/databases/{fake_db_id}/query", method="POST"
    ).respond_with_json({"results": [page_two], "has_more": False, "next_cursor": None})

    result = await verb()

    assert result["results"] == [page_one, page_two]
