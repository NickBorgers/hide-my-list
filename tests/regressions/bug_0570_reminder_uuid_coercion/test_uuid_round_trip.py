"""Regression test: psycopg3 UUID coercion in the reminder worker.

Bug: reminder_worker.py called uuid.UUID(row["id"]) but psycopg3 returns
uuid.UUID objects natively, not strings. This caused AttributeError on delivery,
silently dropping every reminder.

Issue: #570
Fix PR: #571

Requires: DATABASE_URL env var pointing at a Postgres instance with migrations applied.
Skipped automatically when DATABASE_URL is not set.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
pytestmark = pytest.mark.skipif(
    not _HAS_DB,
    reason="DATABASE_URL not set; skipping integration tests",
)

_TEST_PEER = "<test-peer>"


@pytest.fixture()
async def db_conn() -> Any:
    """Provide a clean-state async DB connection with migrations applied."""
    import psycopg

    conn_str = os.environ["DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(conn_str, autocommit=False) as conn:
        # Apply migrations
        from app.tools.db import _MIGRATIONS_DIR
        for mig in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await conn.execute(mig.read_text())  # type: ignore[arg-type]
        await conn.commit()

        # Clean state before each test (child table first to satisfy FK)
        await conn.execute(
            "TRUNCATE reminder_scheduling_ledger, deadline_task_peers, reminder_outbox, "
            "recent_outbound, ops_alerts_throttle"
        )
        await conn.commit()

        yield conn


@pytest.mark.asyncio
async def test_uuid_round_trip_no_attribute_error(db_conn: Any) -> None:
    """Reminder delivery must not raise AttributeError on native UUID row id.

    Regression for bug #570: psycopg3 returns uuid.UUID objects natively.
    The worker's _coerce_uuid() helper must handle both uuid.UUID and str.
    """
    from app.scheduler.reminder_worker import dispatch_due_reminders
    from app.tools import reminders

    # Enqueue via the real enqueue() path so psycopg3 returns a native UUID id.
    rid = await reminders.enqueue(
        db_conn,
        notion_page_id=str(uuid.uuid4()),
        peer=_TEST_PEER,
        body="Test reminder",
        due_at=datetime.now(UTC) - timedelta(seconds=1),  # due now
        idempotency_key=str(uuid.uuid4()),
    )
    await db_conn.commit()

    signal_mock = AsyncMock(return_value={"timestamp": 1234})

    # Must not raise AttributeError — that was the regression.
    await dispatch_due_reminders(db_conn, signal_send_fn=signal_mock)

    # Worker delivered exactly one reminder.
    signal_mock.assert_awaited_once()

    # The mock was called with the correct recipient.
    assert signal_mock.await_args.kwargs["recipient"] == _TEST_PEER

    # The outbox row is now in delivered state.
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT state FROM reminder_outbox WHERE id = %s", (str(rid),)
        )
        row = await cur.fetchone()

    assert row is not None
    assert row[0] == "delivered"
