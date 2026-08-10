"""Unit test: schema-mismatch branch enqueue call-contract.

Pins the kwargs shape of the ops_alerts.enqueue() call made inside the
intentional except-swallow in check_notion_health(). The integration tests
in test_ops_replacements.py assert the resulting DB row; this test asserts
the outbound call contract before the swallow can hide a signature mismatch.

No DATABASE_URL required — all I/O is mocked.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_schema_mismatch_enqueue_call_contract() -> None:
    """schema-mismatch branch calls enqueue with the correct kwargs shape.

    Validates:
    - Every kwarg name matches inspect.signature(ops_alerts.enqueue)
    - kind, body, and severity carry expected values
    """
    from app.scheduler.jobs import check_notion_health
    from app.tools import ops_alerts
    from app.tools.notion import HealthCheckResult, SchemaCheckResult

    mismatch = SchemaCheckResult(
        ok=False,
        missing=("Due At", "Reminder Scheduled At"),
    )
    enqueue_mock = AsyncMock()

    with (
        patch(
            "app.tools.notion.health_check",
            new_callable=AsyncMock,
            return_value=HealthCheckResult(ok=True),
        ),
        patch(
            "app.tools.notion.verify_schema",
            new_callable=AsyncMock,
            return_value=mismatch,
        ),
        patch("app.tools.ops_alerts.enqueue", enqueue_mock),
    ):
        await check_notion_health()

    enqueue_mock.assert_awaited_once()
    kwargs = enqueue_mock.call_args.kwargs

    valid_params = set(inspect.signature(ops_alerts.enqueue).parameters)
    assert set(kwargs) <= valid_params, (
        f"enqueue() called with unknown kwargs: {set(kwargs) - valid_params}"
    )

    assert kwargs.get("kind") == "notion_schema_mismatch"
    assert kwargs.get("severity") == "warning"
    body = kwargs.get("body", "")
    assert "Due At" in body, "alert body must name the missing property"
    assert "Reminder Scheduled At" in body, "alert body must name the missing property"
