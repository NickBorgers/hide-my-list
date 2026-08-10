"""Integration tests for the Notion health probe's failure reporting.

health_check() is the only signal the operator gets that the Notion
integration is broken. It must carry *why* — an alert that says "verify the
key or reachability" names two unrelated causes and is not actionable once
the app logs holding the real exception have rotated or gone unreachable.

Uses mocked httpx via pytest-httpserver — no DATABASE_URL required.

Private data discipline: no real page IDs, titles, or phone numbers.
"""

from __future__ import annotations

import httpx
import pytest
from pytest_httpserver import HTTPServer

import app.tools.notion as notion_module

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def notion_server(httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch) -> HTTPServer:
    """Point the Notion client at the local test server."""
    base_url = httpserver.url_for("/").rstrip("/")
    monkeypatch.setenv("NOTION_API_KEY", "test-api-key")

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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_ok_reports_no_detail(notion_server: HTTPServer) -> None:
    """A 200 from /users/me yields ok=True and no failure detail."""
    notion_server.expect_request("/users/me").respond_with_json({"object": "user"})

    result = await notion_module.health_check()

    assert result.ok is True
    assert result.detail is None


@pytest.mark.asyncio
async def test_health_check_captures_auth_failure_reason(notion_server: HTTPServer) -> None:
    """A 401 must surface as detail naming the status, not a bare False.

    An expired or revoked NOTION_API_KEY is operationally distinct from a
    network outage, and only the detail distinguishes them.
    """
    notion_server.expect_request("/users/me").respond_with_json(
        {"object": "error", "status": 401, "code": "unauthorized"}, status=401
    )

    result = await notion_module.health_check()

    assert result.ok is False
    assert result.detail is not None
    assert "401" in result.detail


@pytest.mark.asyncio
async def test_health_check_captures_transport_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport error must surface as detail, distinct from an HTTP error."""
    monkeypatch.setenv("NOTION_API_KEY", "test-api-key")

    class _FailingClient:
        async def __aenter__(self) -> _FailingClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, _path: str) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(notion_module, "_client_factory", _FailingClient)

    result = await notion_module.health_check()

    assert result.ok is False
    assert result.detail is not None
    assert "ConnectError" in result.detail
    assert "connection refused" in result.detail


@pytest.mark.asyncio
async def test_health_check_detail_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pathological exception repr must not produce an unbounded alert body."""
    monkeypatch.setenv("NOTION_API_KEY", "test-api-key")

    class _ExplodingClient:
        async def __aenter__(self) -> _ExplodingClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, _path: str) -> httpx.Response:
            raise RuntimeError("x" * 5000)

    monkeypatch.setattr(notion_module, "_client_factory", _ExplodingClient)

    result = await notion_module.health_check()

    assert result.ok is False
    assert result.detail is not None
    assert len(result.detail) <= notion_module._HEALTH_DETAIL_MAX_CHARS


def test_health_check_result_requires_explicit_ok_branch() -> None:
    """The result object is always truthy; callers must branch on `.ok`.

    Guards the migration hazard from the old bare-bool return: `if not
    await health_check()` silently became dead code, so a broken Notion
    integration would stop alerting entirely.
    """
    failure = notion_module.HealthCheckResult(ok=False, detail="boom")

    assert bool(failure) is True, (
        "HealthCheckResult is a tuple and always truthy — this test documents "
        "that, so any caller written as `if not result` is caught by review."
    )
    assert failure.ok is False
