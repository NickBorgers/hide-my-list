"""Integration tests for PR-C1 operational replacements.

Tests run against a real Postgres database (set DATABASE_URL in environment)
and use mocked signal_client. No real Signal account required.

Covers:
  - notion_health failure → ops alert enqueued
  - ops_alerts_drain sends via mocked signal client, marks delivered, respects throttle
  - state_audit removes expired rows; idempotent

Private data discipline: all test content uses placeholder strings.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
pytestmark = pytest.mark.skipif(
    not _HAS_DB,
    reason="DATABASE_URL not set; skipping integration tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(UTC)


async def _clean_tables(conn: Any) -> None:
    """Truncate ops_alerts and ops_alerts_throttle before each test."""
    await conn.execute("TRUNCATE ops_alerts, ops_alerts_throttle RESTART IDENTITY CASCADE")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
async def db_conn() -> Any:
    """Provide a clean-state async DB connection for each test.

    autocommit=True is required, not stylistic. The TRUNCATE below takes an
    ACCESS EXCLUSIVE lock; held open in an uncommitted transaction it blocks
    every other connection to these tables. The code under test (ops_alerts
    .enqueue) opens its own connection, so the test would deadlock against
    its own fixture and hang until killed.
    """
    import psycopg
    import psycopg.rows

    conn_str = os.environ["DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(
        conn_str, row_factory=psycopg.rows.dict_row, autocommit=True
    ) as conn:
        await _clean_tables(conn)
        yield conn


# ---------------------------------------------------------------------------
# PR-C1: notion_health failure → ops alert enqueued
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notion_health_failure_enqueues_alert(db_conn: Any) -> None:
    """A failed probe enqueues a critical alert carrying the failure reason.

    The reason must reach the alert body: it is the only copy the operator
    sees over Signal, and the app logs holding the original exception may
    have rotated or become unreachable by the time anyone investigates.
    """
    from app.scheduler.jobs import check_notion_health
    from app.tools.notion import HealthCheckResult

    failure = HealthCheckResult(ok=False, detail="HTTPStatusError: 401 Unauthorized")
    with patch("app.tools.notion.health_check", new_callable=AsyncMock, return_value=failure):
        await check_notion_health()

    # Verify a pending ops alert exists.
    row = await db_conn.execute(
        "SELECT alert_kind, severity, state, body FROM ops_alerts "
        "WHERE alert_kind = 'notion_health_failed'"
    )
    alert = await row.fetchone()
    assert alert is not None, "Expected a pending ops alert for notion_health_failed"
    assert alert["state"] == "pending"
    assert alert["severity"] == "critical"
    assert "401 Unauthorized" in alert["body"], (
        "The probe's failure reason must appear in the alert body. Without it "
        "the operator gets a generic 'verify key or reachability' string that "
        "cannot distinguish an expired token from a network outage."
    )


@pytest.mark.asyncio
async def test_notion_health_failure_without_detail_still_alerts(db_conn: Any) -> None:
    """A failure carrying no detail must still produce an alert, not crash."""
    from app.scheduler.jobs import check_notion_health
    from app.tools.notion import HealthCheckResult

    with patch(
        "app.tools.notion.health_check",
        new_callable=AsyncMock,
        return_value=HealthCheckResult(ok=False),
    ):
        await check_notion_health()

    row = await db_conn.execute(
        "SELECT body FROM ops_alerts WHERE alert_kind = 'notion_health_failed'"
    )
    alert = await row.fetchone()
    assert alert is not None
    assert "no detail captured" in alert["body"]


@pytest.mark.asyncio
async def test_notion_health_ok_no_alert(db_conn: Any) -> None:
    """A healthy probe and a healthy schema create no ops alert."""
    from app.scheduler.jobs import check_notion_health
    from app.tools.notion import HealthCheckResult, SchemaCheckResult

    with (
        patch(
            "app.tools.notion.health_check",
            new_callable=AsyncMock,
            return_value=HealthCheckResult(ok=True),
        ),
        patch(
            "app.tools.notion.verify_schema",
            new_callable=AsyncMock,
            return_value=SchemaCheckResult(ok=True),
        ),
    ):
        await check_notion_health()

    row = await db_conn.execute("SELECT COUNT(*) AS cnt FROM ops_alerts")
    result = await row.fetchone()
    assert result["cnt"] == 0


# ---------------------------------------------------------------------------
# Notion schema mismatch → distinct ops alert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notion_schema_mismatch_enqueues_alert_naming_properties(
    db_conn: Any,
) -> None:
    """A schema mismatch alerts under its own kind, naming the properties.

    The property names must reach the alert body: it is the only copy the
    operator sees over Signal, and an alert saying merely "schema mismatch"
    forces them to reproduce the check by hand before they can act.
    """
    from app.scheduler.jobs import check_notion_health
    from app.tools.notion import HealthCheckResult, SchemaCheckResult

    mismatch = SchemaCheckResult(
        ok=False,
        missing=("Due At", "Reminder Scheduled At"),
    )
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
    ):
        await check_notion_health()

    row = await db_conn.execute(
        "SELECT alert_kind, severity, state, body FROM ops_alerts "
        "WHERE alert_kind = 'notion_schema_mismatch'"
    )
    alert = await row.fetchone()
    assert alert is not None, "Expected a pending ops alert for notion_schema_mismatch"
    assert alert["state"] == "pending"
    assert alert["severity"] == "warning"
    assert "Due At" in alert["body"]
    assert "Reminder Scheduled At" in alert["body"]


@pytest.mark.asyncio
async def test_connectivity_failure_skips_schema_probe(db_conn: Any) -> None:
    """When the API is unreachable the schema probe does not run.

    It could not read the schema during an outage anyway, and a second alert
    would describe the same root cause twice — doubling the noise at exactly
    the moment the operator needs a clear signal.
    """
    from app.scheduler.jobs import check_notion_health
    from app.tools.notion import HealthCheckResult

    verify = AsyncMock()
    with (
        patch(
            "app.tools.notion.health_check",
            new_callable=AsyncMock,
            return_value=HealthCheckResult(ok=False, detail="ConnectError: unreachable"),
        ),
        patch("app.tools.notion.verify_schema", verify),
    ):
        await check_notion_health()

    verify.assert_not_awaited()

    row = await db_conn.execute(
        "SELECT COUNT(*) AS cnt FROM ops_alerts WHERE alert_kind = 'notion_schema_mismatch'"
    )
    result = await row.fetchone()
    assert result["cnt"] == 0


# ---------------------------------------------------------------------------
# PR-C1: ops_alerts_drain sends via mocked signal client, marks delivered
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ops_alerts_drain_delivers_and_marks(db_conn: Any) -> None:
    """drain() sends pending alert via signal_client and marks it delivered."""
    from app.tools import ops_alerts

    # Insert a pending alert directly.
    alert_id = uuid.uuid4()
    await db_conn.execute(
        """
        INSERT INTO ops_alerts (id, alert_kind, body, severity, state, created_at)
        VALUES (%s, 'test_kind', 'Test alert body', 'warning', 'pending', %s)
        """,
        (alert_id, _now()),
    )

    mock_send = AsyncMock(return_value={"timestamp": 12345})

    with (
        patch.dict(os.environ, {"OPS_ALERT_SIGNAL_NUMBER": "+10000000000"}),
        patch("app.tools.signal_client.send_message", mock_send),
    ):
        await ops_alerts.drain()

    # Verify signal_client was called once.
    assert mock_send.call_count == 1
    call_kwargs = mock_send.call_args
    assert "+10000000000" in str(call_kwargs)
    assert "Test alert body" in str(call_kwargs)

    # Verify alert is now marked delivered.
    row = await db_conn.execute(
        "SELECT state, delivered_at FROM ops_alerts WHERE id = %s", (alert_id,)
    )
    alert = await row.fetchone()
    assert alert is not None
    assert alert["state"] == "delivered"
    assert alert["delivered_at"] is not None


@pytest.mark.asyncio
async def test_ops_alerts_drain_respects_throttle(db_conn: Any) -> None:
    """drain() marks alert throttled when kind was recently delivered."""
    from app.tools import ops_alerts

    kind = "throttle_test_kind"
    alert_id = uuid.uuid4()

    # Insert a pending alert.
    await db_conn.execute(
        """
        INSERT INTO ops_alerts (id, alert_kind, body, severity, state, created_at)
        VALUES (%s, %s, 'Throttle test body', 'warning', 'pending', %s)
        """,
        (alert_id, kind, _now()),
    )

    # Simulate that this kind was delivered 30 minutes ago (within 1h throttle window).
    recent = _now() - timedelta(minutes=30)
    await db_conn.execute(
        "INSERT INTO ops_alerts_throttle (alert_kind, last_sent_at) VALUES (%s, %s)",
        (kind, recent),
    )

    mock_send = AsyncMock()

    with (
        patch.dict(os.environ, {"OPS_ALERT_SIGNAL_NUMBER": "+10000000000"}),
        patch("app.tools.signal_client.send_message", mock_send),
    ):
        await ops_alerts.drain()

    # Signal should NOT have been called.
    mock_send.assert_not_called()

    # Alert should be marked throttled.
    row = await db_conn.execute(
        "SELECT state FROM ops_alerts WHERE id = %s", (alert_id,)
    )
    alert = await row.fetchone()
    assert alert is not None
    assert alert["state"] == "throttled"


@pytest.mark.asyncio
async def test_ops_alerts_drain_no_recipient_skips_gracefully(db_conn: Any) -> None:
    """drain() skips delivery and logs when OPS_ALERT_SIGNAL_NUMBER is not set."""
    from app.tools import ops_alerts

    alert_id = uuid.uuid4()
    await db_conn.execute(
        """
        INSERT INTO ops_alerts (id, alert_kind, body, severity, state, created_at)
        VALUES (%s, 'no_recipient', 'Body', 'warning', 'pending', %s)
        """,
        (alert_id, _now()),
    )

    mock_send = AsyncMock()
    env_without_recipient = {k: v for k, v in os.environ.items()
                             if k != "OPS_ALERT_SIGNAL_NUMBER"}

    with (
        patch.dict(os.environ, env_without_recipient, clear=True),
        patch("app.tools.signal_client.send_message", mock_send),
    ):
        # Should not raise; logs error and returns.
        await ops_alerts.drain()

    mock_send.assert_not_called()

    # Alert remains pending (drain exited early).
    row = await db_conn.execute(
        "SELECT state FROM ops_alerts WHERE id = %s", (alert_id,)
    )
    alert = await row.fetchone()
    assert alert is not None
    assert alert["state"] == "pending"


# ---------------------------------------------------------------------------
# PR-C1: state_audit removes expired rows; idempotent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_state_audit_prunes_expired_recent_outbound(db_conn: Any) -> None:
    """state_audit removes recent_outbound rows past their expires_at."""
    from app.scheduler.jobs import run_state_audit

    now = _now()
    expired = now - timedelta(days=91)
    fresh = now + timedelta(hours=1)

    # Insert expired and fresh rows.
    await db_conn.execute(
        """
        INSERT INTO recent_outbound
          (peer, signal_timestamp, notion_page_id, sent_at, awaiting_reply, expires_at)
        VALUES
          ('+10000000001', 1000, 'page-expired', %s, true, %s),
          ('+10000000002', 2000, 'page-fresh',   %s, true, %s)
        """,
        (now, expired, now, fresh),
    )

    await run_state_audit()

    # Expired row should be gone.
    row = await db_conn.execute(
        "SELECT notion_page_id FROM recent_outbound"
    )
    rows = await row.fetchall()
    page_ids = [r["notion_page_id"] for r in rows]
    assert "page-expired" not in page_ids
    assert "page-fresh" in page_ids


@pytest.mark.asyncio
async def test_state_audit_idempotent(db_conn: Any) -> None:
    """Running state_audit twice does not raise or produce errors."""
    from app.scheduler.jobs import run_state_audit

    await run_state_audit()
    # Second run — should be a no-op.
    await run_state_audit()


