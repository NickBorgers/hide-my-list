"""Unit tests for the reminder delivery worker."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest


class FakeConnection:
    """Minimal async connection surface used by dispatch_due_reminders."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def execute(
        self,
        query: str,
        params: tuple[Any, ...] | None = None,
    ) -> None:
        self.executed.append((query, params))


@pytest.mark.asyncio
async def test_dispatch_due_reminders_accepts_native_uuid_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """psycopg maps Postgres UUID columns to uuid.UUID objects."""
    from app.scheduler import reminder_worker
    from app.tools import notion

    reminder_id = uuid.uuid4()
    idempotency_key = str(uuid.uuid4())
    row = {
        "id": reminder_id,
        "peer": "<peer>",
        "body": "Test reminder",
        "notion_page_id": "<page-id>",
        "idempotency_key": idempotency_key,
        "attempt": 0,
        "due_at": datetime.now(UTC) - timedelta(seconds=1),
    }

    async def fake_claim_due_reminders(
        conn: Any,
        worker_id: str,
    ) -> list[dict[str, Any]]:
        return [row]

    monkeypatch.setattr(reminder_worker, "_claim_due_reminders", fake_claim_due_reminders)
    complete_reminder = AsyncMock(return_value={})
    monkeypatch.setattr(notion, "complete_reminder", complete_reminder)

    signal_send = AsyncMock(return_value={"timestamp": 12345})
    conn = FakeConnection()

    await reminder_worker.dispatch_due_reminders(conn, signal_send_fn=signal_send)

    signal_send.assert_awaited_once_with(
        recipient="<peer>",
        message="Test reminder",
        idempotency_key=idempotency_key,
    )
    complete_reminder.assert_awaited_once_with("<page-id>", "sent")
    assert conn.rollbacks == 0
    assert any(params is not None and params[-1] == str(reminder_id) for _, params in conn.executed)


@pytest.mark.asyncio
async def test_dispatch_skips_complete_reminder_for_deadline_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deadline rows point at task pages, so delivery must not complete them."""
    from app.scheduler import reminder_worker
    from app.tools import notion

    reminder_id = uuid.uuid4()
    row = {
        "id": reminder_id,
        "peer": "<peer>",
        "body": "Deadline check-in.",
        "notion_page_id": "<page-id>",
        "idempotency_key": str(uuid.uuid4()),
        "attempt": 0,
        "due_at": datetime.now(UTC) - timedelta(seconds=1),
        "kind": "deadline",
    }

    async def fake_claim_due_reminders(
        conn: Any,
        worker_id: str,
    ) -> list[dict[str, Any]]:
        return [row]

    monkeypatch.setattr(reminder_worker, "_claim_due_reminders", fake_claim_due_reminders)
    complete_reminder = AsyncMock(return_value={})
    monkeypatch.setattr(notion, "complete_reminder", complete_reminder)

    signal_send = AsyncMock(return_value={"timestamp": 12345})
    conn = FakeConnection()

    await reminder_worker.dispatch_due_reminders(conn, signal_send_fn=signal_send)

    signal_send.assert_awaited_once()
    kwargs = signal_send.await_args.kwargs
    assert kwargs["recipient"] == "<peer>"
    assert kwargs["message"] == "Deadline check-in."
    assert kwargs["idempotency_key"] == row["idempotency_key"]
    complete_reminder.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_treats_missing_kind_as_reminder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows missing the field at mapping level keep legacy completion behavior."""
    from app.scheduler import reminder_worker
    from app.tools import notion

    reminder_id = uuid.uuid4()
    row = {
        "id": reminder_id,
        "peer": "<peer>",
        "body": "Legacy reminder",
        "notion_page_id": "<page-id>",
        "idempotency_key": str(uuid.uuid4()),
        "attempt": 0,
        "due_at": datetime.now(UTC) - timedelta(seconds=1),
    }

    async def fake_claim_due_reminders(
        conn: Any,
        worker_id: str,
    ) -> list[dict[str, Any]]:
        return [row]

    monkeypatch.setattr(reminder_worker, "_claim_due_reminders", fake_claim_due_reminders)
    complete_reminder = AsyncMock(return_value={})
    monkeypatch.setattr(notion, "complete_reminder", complete_reminder)

    signal_send = AsyncMock(return_value={"timestamp": 12345})
    conn = FakeConnection()

    await reminder_worker.dispatch_due_reminders(conn, signal_send_fn=signal_send)

    complete_reminder.assert_awaited_once_with("<page-id>", "sent")
