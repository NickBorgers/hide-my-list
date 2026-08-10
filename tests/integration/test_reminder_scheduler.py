"""Integration-style tests for the deadline scheduler backstop."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest


def _page(page_id: str, due_at: datetime, urgency: int = 50) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Due At": {"date": {"start": due_at.isoformat()}},
            "Urgency": {"number": urgency},
        },
    }


@pytest.mark.asyncio
async def test_reminder_scheduler_job_invokes_backstop(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.scheduler.reminder_scheduler as scheduler_module
    from app.scheduler.jobs import _USER_TZ, run_reminder_scheduler_job

    run_backstop = AsyncMock(return_value=None)
    monkeypatch.setattr(scheduler_module, "run_reminder_scheduler", run_backstop)

    await run_reminder_scheduler_job()

    run_backstop.assert_awaited_once_with(user_tz=_USER_TZ)


@pytest.mark.asyncio
async def test_orphan_catchup_schedules_and_marks(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.scheduler import reminder_scheduler
    from app.tools import notion

    page = _page("<page-id>", datetime.now(UTC) + timedelta(days=5))
    monkeypatch.setattr(
        notion,
        "query_tasks_with_unscheduled_deadlines",
        AsyncMock(return_value={"results": [page]}),
    )
    monkeypatch.setattr(
        notion,
        "query_scheduled_tasks_with_deadlines",
        AsyncMock(return_value={"results": []}),
    )
    mark_scheduled = AsyncMock(return_value={})
    monkeypatch.setattr(notion, "mark_reminder_scheduled", mark_scheduled)

    scheduled_calls: list[dict[str, Any]] = []

    async def fake_schedule_for_task(conn: Any, **kwargs: Any) -> tuple[list[Any], list[str]]:
        scheduled_calls.append(kwargs)
        return [type("Scheduled", (), {"label": "3d", "assigned_at": datetime.now(UTC)})()], []

    monkeypatch.setattr(reminder_scheduler, "schedule_for_task", fake_schedule_for_task)
    monkeypatch.setattr(
        reminder_scheduler,
        "get_active_deadline_for_page",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        reminder_scheduler,
        "get_peer_for_task",
        AsyncMock(return_value="<recipient>"),
    )

    class FakeCtx:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr("app.tools.db.get_db_conn", lambda: FakeCtx())

    await reminder_scheduler.run_reminder_scheduler(user_tz="America/Chicago")

    assert len(scheduled_calls) == 1
    assert scheduled_calls[0]["notion_page_id"] == "<page-id>"
    mark_scheduled.assert_awaited_once_with("<page-id>")


@pytest.mark.asyncio
async def test_deadline_edit_detection_supersedes_and_reschedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.scheduler import reminder_scheduler
    from app.tools import notion

    current_deadline = datetime.now(UTC) + timedelta(days=8)
    page = _page("<page-id>", current_deadline)
    monkeypatch.setattr(
        notion,
        "query_tasks_with_unscheduled_deadlines",
        AsyncMock(return_value={"results": []}),
    )
    monkeypatch.setattr(
        notion,
        "query_scheduled_tasks_with_deadlines",
        AsyncMock(return_value={"results": [page]}),
    )
    mark_scheduled = AsyncMock(return_value={})
    monkeypatch.setattr(notion, "mark_reminder_scheduled", mark_scheduled)

    monkeypatch.setattr(
        reminder_scheduler,
        "get_active_deadline_for_page",
        AsyncMock(return_value=current_deadline - timedelta(days=1)),
    )
    monkeypatch.setattr(
        reminder_scheduler,
        "get_peer_for_task",
        AsyncMock(return_value="<recipient>"),
    )
    supersede = AsyncMock(return_value=[])
    cancel = AsyncMock(return_value=None)
    monkeypatch.setattr(reminder_scheduler, "supersede_ledger_rows", supersede)
    monkeypatch.setattr(reminder_scheduler, "cancel_outbox_rows", cancel)

    async def fake_schedule_for_task(conn: Any, **kwargs: Any) -> tuple[list[Any], list[str]]:
        return [type("Scheduled", (), {"label": "3d", "assigned_at": datetime.now(UTC)})()], []

    monkeypatch.setattr(reminder_scheduler, "schedule_for_task", fake_schedule_for_task)

    class FakeConn:
        async def commit(self) -> None:
            return None

    class FakeCtx:
        async def __aenter__(self) -> FakeConn:
            return FakeConn()

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr("app.tools.db.get_db_conn", lambda: FakeCtx())

    await reminder_scheduler.run_reminder_scheduler(user_tz="America/Chicago")

    supersede.assert_awaited_once()
    cancel.assert_awaited_once()
    mark_scheduled.assert_awaited_once_with("<page-id>")


@pytest.mark.asyncio
async def test_orphan_without_recorded_peer_alerts_and_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.scheduler import reminder_scheduler
    from app.tools import notion, ops_alerts

    page = _page("<page-id>", datetime.now(UTC) + timedelta(days=5))
    monkeypatch.setattr(
        notion,
        "query_tasks_with_unscheduled_deadlines",
        AsyncMock(return_value={"results": [page]}),
    )
    monkeypatch.setattr(
        notion,
        "query_scheduled_tasks_with_deadlines",
        AsyncMock(return_value={"results": []}),
    )
    monkeypatch.setattr(notion, "mark_reminder_scheduled", AsyncMock(return_value={}))
    schedule_for_task = AsyncMock(return_value=([], []))
    monkeypatch.setattr(reminder_scheduler, "schedule_for_task", schedule_for_task)
    monkeypatch.setattr(
        reminder_scheduler,
        "get_peer_for_task",
        AsyncMock(return_value=None),
    )
    alert = AsyncMock(return_value=None)
    monkeypatch.setattr(ops_alerts, "enqueue", alert)

    class FakeCtx:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr("app.tools.db.get_db_conn", lambda: FakeCtx())

    await reminder_scheduler.run_reminder_scheduler(user_tz="America/Chicago")

    schedule_for_task.assert_not_awaited()
    alert.assert_awaited_once()
