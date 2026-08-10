"""Integration tests for deadline reminder scheduling DB writes."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
pytestmark = pytest.mark.skipif(
    not _HAS_DB,
    reason="DATABASE_URL not set; skipping integration tests",
)


@pytest.fixture()
async def db_conn() -> Any:
    import psycopg

    conn_str = os.environ["DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(conn_str, autocommit=False) as conn:
        from app.tools.db import _MIGRATIONS_DIR

        for mig in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await conn.execute(mig.read_text())  # type: ignore[arg-type]
        await conn.commit()
        await conn.execute(
            "TRUNCATE reminder_scheduling_ledger, deadline_task_peers, reminder_outbox, "
            "recent_outbound, ops_alerts_throttle"
        )
        await conn.commit()
        yield conn


@pytest.mark.asyncio
async def test_schedule_for_task_writes_deadline_outbox_and_ledger(db_conn: Any) -> None:
    from app.scheduler.reminder_scheduling import get_active_deadline_for_page, schedule_for_task

    now = datetime.now(UTC)
    deadline = now + timedelta(days=5)

    scheduled, failures = await schedule_for_task(
        db_conn,
        notion_page_id="<page-id>",
        peer="<recipient>",
        deadline_at=deadline,
        urgency=50,
        now=now,
        user_tz="America/Chicago",
    )

    assert failures == []
    assert [item.label for item in scheduled] == ["3d", "1d", "4h"]

    async with db_conn.cursor() as cur:
        await cur.execute(
            """
            SELECT o.kind, o.notion_page_id, o.peer, l.milestone_label, l.deadline_at
              FROM reminder_outbox o
              JOIN reminder_scheduling_ledger l ON l.reminder_outbox_id = o.id
             ORDER BY l.assigned_slot_at
            """
        )
        rows = await cur.fetchall()

    assert len(rows) == 3
    assert {row[0] for row in rows} == {"deadline"}
    assert {row[1] for row in rows} == {"<page-id>"}
    assert {row[2] for row in rows} == {"<recipient>"}
    assert [row[3] for row in rows] == ["3d", "1d", "4h"]

    active_deadline = await get_active_deadline_for_page(db_conn, "<page-id>")
    assert active_deadline is not None
    assert abs((active_deadline - deadline).total_seconds()) < 1


@pytest.mark.asyncio
async def test_supersede_marks_ledger_and_deadens_outbox(db_conn: Any) -> None:
    from app.scheduler.reminder_scheduling import (
        cancel_outbox_rows,
        get_active_deadline_for_page,
        schedule_for_task,
        supersede_ledger_rows,
    )

    now = datetime.now(UTC)
    await schedule_for_task(
        db_conn,
        notion_page_id="<page-id>",
        peer="<recipient>",
        deadline_at=now + timedelta(days=5),
        urgency=50,
        now=now,
        user_tz="America/Chicago",
    )

    outbox_ids = await supersede_ledger_rows(db_conn, "<page-id>")
    await cancel_outbox_rows(db_conn, outbox_ids)
    await db_conn.commit()

    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM reminder_scheduling_ledger WHERE superseded_at IS NOT NULL"
        )
        superseded_count = (await cur.fetchone())[0]
        await cur.execute("SELECT COUNT(*) FROM reminder_outbox WHERE state = 'dead'")
        dead_count = (await cur.fetchone())[0]

    assert superseded_count == len(outbox_ids)
    assert dead_count == len(outbox_ids)
    assert await get_active_deadline_for_page(db_conn, "<page-id>") is None


@pytest.mark.asyncio
async def test_deadline_task_peer_round_trip(db_conn: Any) -> None:
    from app.scheduler.reminder_scheduling import get_peer_for_task, record_deadline_task_peer

    await record_deadline_task_peer(
        db_conn,
        notion_page_id="<page-id>",
        peer="<recipient>",
    )

    assert await get_peer_for_task(db_conn, "<page-id>") == "<recipient>"
    assert await get_peer_for_task(db_conn, "<missing-page-id>") is None
