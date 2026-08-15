"""Regression: COMPLETE resolves an unresolved reminder before stale active_task."""
from __future__ import annotations

import inspect
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))


def _assert_reward_kwargs_shape(kwargs: dict[str, Any]) -> None:
    from app.tools import rewards

    assert set(kwargs) <= set(inspect.signature(rewards.maybe_reward).parameters)
    assert set(kwargs) == {
        "peer",
        "task_title",
        "notion_page_id",
        "streak",
        "work_type",
        "energy_required",
    }


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


def _state(active_task: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "peer": "<recipient>",
        "incoming": "done",
        "intent": "COMPLETE",
        "messages": [],
        "active_task": active_task,
        "streak": 4,
        "tasks_completed_today": 1,
        "user_prefs": {},
        "mood": None,
        "available_minutes": None,
        "conversation_state": "active",
        "pending_outbound": [],
    }


@pytest.mark.asyncio
async def test_complete_prefers_unresolved_recent_outbound_over_stale_active_task() -> None:
    from app.graph.nodes import complete as complete_module

    stale_active_task = {
        "page_id": "<page_B>",
        "title": "",
        "status": "In Progress",
        "selected_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
        "work_type": "focus",
        "energy_required": "Medium",
    }
    recent_target = complete_module._CompletionTarget(
        source="recent_outbound",
        page_id="<page_A>",
        task_title="Test reminder",
        work_type="",
        energy_required="",
        context_at=datetime.now(UTC) - timedelta(hours=2),
        signal_timestamp=123456789,
    )
    reward_mock = AsyncMock(
        return_value={"text": "Nice work!", "attachment_path": None}
    )
    clear_mock = AsyncMock()

    with (
        patch("app.tools.notion.update_status", new_callable=AsyncMock) as update_status,
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(
            complete_module,
            "_load_recent_outbound_target",
            AsyncMock(return_value=recent_target),
        ),
        patch.object(complete_module, "_clear_recent_outbound", clear_mock),
    ):
        result = await complete_module.complete_node(_state(stale_active_task))  # type: ignore[arg-type]

    update_status.assert_not_awaited()
    reward_mock.assert_awaited_once()
    reward_kwargs = reward_mock.await_args.kwargs
    _assert_reward_kwargs_shape(reward_kwargs)
    assert reward_kwargs == {
        "peer": "<recipient>",
        "task_title": "Test reminder",
        "notion_page_id": "<page_A>",
        "streak": 5,
        "work_type": "",
        "energy_required": "",
    }
    # The page id is passed so the clear resolves every live row for that task,
    # not only the delivery the user replied to. A sibling row left behind is an
    # orphan a later unrelated "done" would resolve.
    clear_mock.assert_awaited_once_with("<recipient>", 123456789, "<page_A>")
    assert result["active_task"] is None
    assert result["streak"] == 5
    assert result["pending_outbound"][0]["notion_page_id"] == "<page_A>"


@pytest.mark.skipif(not _HAS_DB, reason="DATABASE_URL not set; skipping DB-backed regression")
@pytest.mark.asyncio
async def test_complete_reads_and_clears_recent_outbound_row(db_conn: Any) -> None:
    from app.graph.nodes import complete as complete_module

    active_page_id = str(uuid.uuid4())
    reminder_page_id = str(uuid.uuid4())
    peer = "<recipient>"
    signal_timestamp = 641000
    stale_active_task = {
        "page_id": active_page_id,
        "title": "",
        "status": "In Progress",
        "selected_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
    }

    await db_conn.execute(
        """
        INSERT INTO recent_outbound
          (peer, signal_timestamp, notion_page_id, reminder_type, title,
           prompt_kind, sent_at, awaiting_reply, expires_at)
        VALUES (%s, %s, %s, 'reminder', %s, 'sent', now(), true,
                now() + interval '24 hours')
        """,
        (peer, signal_timestamp, reminder_page_id, "Test reminder"),
    )
    await db_conn.commit()

    reward_mock = AsyncMock(
        return_value={"text": "Nice work!", "attachment_path": None}
    )

    with (
        patch("app.tools.notion.update_status", new_callable=AsyncMock) as update_status,
        patch("app.tools.rewards.maybe_reward", reward_mock),
    ):
        await complete_module.complete_node(
            {
                **_state(stale_active_task),
                "peer": peer,
            }  # type: ignore[arg-type]
        )

    update_status.assert_not_awaited()
    reward_mock.assert_awaited_once()
    assert reward_mock.await_args.kwargs["notion_page_id"] == reminder_page_id

    async with db_conn.cursor() as cur:
        await cur.execute(
            """
            SELECT awaiting_reply
              FROM recent_outbound
             WHERE peer = %s
               AND signal_timestamp = %s
            """,
            (peer, signal_timestamp),
        )
        row = await cur.fetchone()

    assert row is not None
    assert row[0] is False


@pytest.mark.asyncio
async def test_complete_asks_when_active_task_has_no_selected_at() -> None:
    from app.graph.nodes import complete as complete_module

    active_task = {
        "page_id": "<page_B>",
        "title": "Test task",
        "status": "In Progress",
    }

    with (
        patch("app.tools.notion.update_status", new_callable=AsyncMock) as update_status,
        patch("app.tools.rewards.maybe_reward", new_callable=AsyncMock) as reward_mock,
        patch.object(
            complete_module,
            "_load_recent_outbound_target",
            AsyncMock(return_value=None),
        ),
    ):
        result = await complete_module.complete_node(_state(active_task))  # type: ignore[arg-type]

    update_status.assert_not_awaited()
    reward_mock.assert_not_awaited()
    assert result["active_task"] is None
    assert result["pending_outbound"][0]["notion_page_id"] is None
    assert "Which task" in result["pending_outbound"][0]["body"]


@pytest.mark.asyncio
async def test_complete_uses_fresh_active_task_when_recent_outbound_lookup_fails() -> None:
    from app.graph.nodes import complete as complete_module

    active_task = {
        "page_id": "<page_B>",
        "title": "Test task",
        "status": "In Progress",
        "selected_at": datetime.now(UTC).isoformat(),
        "work_type": "focus",
        "energy_required": "Medium",
    }
    reward_mock = AsyncMock(
        return_value={"text": "Nice work!", "attachment_path": None}
    )

    with (
        patch("app.tools.notion.update_status", new_callable=AsyncMock) as update_status,
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(
            complete_module,
            "_load_recent_outbound_target",
            AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),
    ):
        result = await complete_module.complete_node(_state(active_task))  # type: ignore[arg-type]

    update_status.assert_awaited_once_with(page_id="<page_B>", new_status="Completed")
    reward_mock.assert_awaited_once()
    reward_kwargs = reward_mock.await_args.kwargs
    _assert_reward_kwargs_shape(reward_kwargs)
    assert reward_kwargs == {
        "peer": "<recipient>",
        "task_title": "Test task",
        "notion_page_id": "<page_B>",
        "streak": 5,
        "work_type": "focus",
        "energy_required": "Medium",
    }
    assert result["pending_outbound"][0]["notion_page_id"] == "<page_B>"


@pytest.mark.asyncio
async def test_complete_asks_when_only_active_task_is_expired() -> None:
    from app.graph.nodes import complete as complete_module

    stale_active_task = {
        "page_id": "<page_B>",
        "title": "Test task",
        "status": "In Progress",
        "selected_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
    }

    with (
        patch("app.tools.notion.update_status", new_callable=AsyncMock) as update_status,
        patch("app.tools.rewards.maybe_reward", new_callable=AsyncMock) as reward_mock,
        patch.object(
            complete_module,
            "_load_recent_outbound_target",
            AsyncMock(return_value=None),
        ),
    ):
        result = await complete_module.complete_node(_state(stale_active_task))  # type: ignore[arg-type]

    update_status.assert_not_awaited()
    reward_mock.assert_not_awaited()
    assert result["active_task"] is None
    assert result["pending_outbound"][0]["notion_page_id"] is None
    assert "Which task" in result["pending_outbound"][0]["body"]
