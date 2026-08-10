"""Integration tests for verify_schema() — the Notion database schema probe.

health_check() proves the token works and the API is reachable. It says
nothing about the database being queried, so a database missing a property
the client filters on passes the health probe and fails only when a verb
touching that property runs. For the nightly deadline scheduler that is up
to 24 hours after deploy, and it arrives as an opaque 400.

verify_schema() closes that window. These tests pin what it must report:
the specific property names, so the ops alert is actionable without first
reproducing the failure.

Uses mocked httpx via pytest-httpserver — no DATABASE_URL required.

Private data discipline: no real page IDs, titles, or phone numbers.
"""

from __future__ import annotations

import httpx
import pytest
from pytest_httpserver import HTTPServer

import app.tools.notion as notion_module

_TEST_DB_ID = "00000000000000000000000000000000"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def notion_server(httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch) -> HTTPServer:
    """Point the Notion client at the local test server."""
    base_url = httpserver.url_for("/").rstrip("/")
    monkeypatch.setenv("NOTION_API_KEY", "test-api-key")
    monkeypatch.setenv("NOTION_DATABASE_ID", _TEST_DB_ID)

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


def _schema_payload(overrides: dict[str, str | None] | None = None) -> dict[str, object]:
    """Build a database response exposing every required property.

    `overrides` maps a property name to a replacement type, or to None to
    drop the property entirely.
    """
    overrides = overrides or {}
    props: dict[str, object] = {}
    for name, prop_type in notion_module.REQUIRED_PROPERTIES.items():
        if name in overrides:
            replacement = overrides[name]
            if replacement is None:
                continue
            props[name] = {"id": "abc", "name": name, "type": replacement}
        else:
            props[name] = {"id": "abc", "name": name, "type": prop_type}
    return {"object": "database", "id": _TEST_DB_ID, "properties": props}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_schema_passes(notion_server: HTTPServer) -> None:
    """A database exposing every required property reports ok with no findings."""
    notion_server.expect_request(f"/databases/{_TEST_DB_ID}").respond_with_json(
        _schema_payload()
    )

    result = await notion_module.verify_schema()

    assert result.ok is True
    assert result.missing == ()
    assert result.mistyped == ()
    assert result.detail is None


@pytest.mark.asyncio
async def test_missing_property_is_named(notion_server: HTTPServer) -> None:
    """The deadline properties absent from the database are named in the result.

    This is the regression case: the nightly deadline scheduler 400s because
    the database has no `Due At`. The operator must learn *which* property is
    missing, not merely that something is wrong.
    """
    notion_server.expect_request(f"/databases/{_TEST_DB_ID}").respond_with_json(
        _schema_payload({"Due At": None, "Reminder Scheduled At": None})
    )

    result = await notion_module.verify_schema()

    assert result.ok is False
    assert set(result.missing) == {"Due At", "Reminder Scheduled At"}
    assert result.mistyped == ()
    assert "Due At" in result.summary()
    assert "Reminder Scheduled At" in result.summary()


@pytest.mark.asyncio
async def test_wrong_type_is_named_with_both_types(notion_server: HTTPServer) -> None:
    """A property of the wrong type reports expected and actual.

    A `Due At` stored as rich_text is present but unusable: a date filter
    against it 400s exactly as a missing property does, so presence alone is
    not a sufficient check.
    """
    notion_server.expect_request(f"/databases/{_TEST_DB_ID}").respond_with_json(
        _schema_payload({"Due At": "rich_text"})
    )

    result = await notion_module.verify_schema()

    assert result.ok is False
    assert result.missing == ()
    assert len(result.mistyped) == 1
    finding = result.mistyped[0]
    assert "Due At" in finding
    assert "date" in finding
    assert "rich_text" in finding


@pytest.mark.asyncio
async def test_missing_and_mistyped_reported_together(notion_server: HTTPServer) -> None:
    """Both problem kinds surface in one pass — no short-circuit on the first."""
    notion_server.expect_request(f"/databases/{_TEST_DB_ID}").respond_with_json(
        _schema_payload({"Due At": None, "Urgency": "rich_text"})
    )

    result = await notion_module.verify_schema()

    assert result.ok is False
    assert result.missing == ("Due At",)
    assert len(result.mistyped) == 1
    assert "Urgency" in result.mistyped[0]


@pytest.mark.asyncio
async def test_unreadable_schema_reports_detail_not_missing(
    notion_server: HTTPServer,
) -> None:
    """An HTTP failure is reported as `detail`, with missing/mistyped empty.

    "We could not read the schema" and "the schema is wrong" call for
    different operator responses. Reporting an unreadable database as though
    every property were missing would be actively misleading.
    """
    notion_server.expect_request(f"/databases/{_TEST_DB_ID}").respond_with_data(
        "Unauthorized", status=401
    )

    result = await notion_module.verify_schema()

    assert result.ok is False
    assert result.missing == ()
    assert result.mistyped == ()
    assert result.detail is not None
    assert "401" in result.detail


@pytest.mark.asyncio
async def test_verify_schema_does_not_raise_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport error returns a result rather than raising.

    The caller is a scheduler job, where an exception crashes the job instead
    of marking it failed — the same constraint health_check() is built to.
    """
    monkeypatch.setenv("NOTION_API_KEY", "test-api-key")
    monkeypatch.setenv("NOTION_DATABASE_ID", _TEST_DB_ID)

    def _unreachable_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://127.0.0.1:1",
            timeout=httpx.Timeout(connect=0.2, read=0.2, write=0.2, pool=0.2),
        )

    monkeypatch.setattr(notion_module, "_client_factory", _unreachable_client)

    result = await notion_module.verify_schema()

    assert result.ok is False
    assert result.detail is not None


@pytest.mark.asyncio
async def test_empty_properties_object_reports_all_missing(
    notion_server: HTTPServer,
) -> None:
    """A database with no properties reports every required name as missing."""
    notion_server.expect_request(f"/databases/{_TEST_DB_ID}").respond_with_json(
        {"object": "database", "id": _TEST_DB_ID, "properties": {}}
    )

    result = await notion_module.verify_schema()

    assert result.ok is False
    assert set(result.missing) == set(notion_module.REQUIRED_PROPERTIES)


def test_schema_check_result_requires_explicit_ok_branch() -> None:
    """SchemaCheckResult is a NamedTuple and therefore always truthy.

    `if not await verify_schema():` would silently never fire. Callers must
    branch on `.ok`. This mirrors the same guard on HealthCheckResult.
    """
    failure = notion_module.SchemaCheckResult(ok=False, missing=("Due At",))

    assert bool(failure) is True, (
        "SchemaCheckResult is a tuple and always truthy — this test documents "
        "why callers must branch on .ok rather than on the result itself"
    )


def test_summary_of_healthy_result_is_ok() -> None:
    """A passing result summarises as 'ok' rather than an empty string."""
    assert notion_module.SchemaCheckResult(ok=True).summary() == "ok"
