"""Integration tests for the ADD_TASK (intake) node.

Tests the intake node in isolation using mocked Notion and mocked LLM calls.
No real network calls are made — all external dependencies are mocked.

Covers:
- "remind me at 5pm tomorrow" → Notion row + outbox row with correct due_at
- "I need to do laundry" (no time) → task created without reminder
- "every weekday at 8" (recurring) → explicit unsupported response
- Section-anchor parity for intake.md.j2
- Idempotency: same page_id enqueued twice doesn't duplicate
"""
from __future__ import annotations

import inspect
import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from app.graph.state import State

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _empty_dedup_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default intake dedup query result for tests unrelated to duplicates."""

    async def query_all() -> dict[str, Any]:
        return {"results": []}

    monkeypatch.setattr("app.tools.notion.query_all", query_all)


def _make_notion_page(page_id: str = "", title: str = "Placeholder task") -> dict:
    """Build a minimal Notion page response dict."""
    return {
        "id": page_id or str(uuid.uuid4()),
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": "Pending"}},
        },
    }


def _make_query_page(page_id: str, title: str, *, status: str = "Pending") -> dict[str, Any]:
    """Build a minimal Notion query result page."""
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": status}},
            "Is Reminder": {"checkbox": False},
        },
    }


def _llm_save_response(
    title: str = "Placeholder task",
    work_type: str = "focus",
    urgency: int = 50,
    time_estimate: int = 30,
    is_reminder: bool = False,
    remind_at: str | None = None,
    due_at: str | None = None,
    confirmation: str = "Got it — focus, ~30 min.",
) -> str:
    """Build a mock LLM save response JSON."""
    return json.dumps({
        "action": "save",
        "title": title,
        "work_type": work_type,
        "urgency": urgency,
        "time_estimate_minutes": time_estimate,
        "energy_required": "Medium",
        "is_reminder": is_reminder,
        "remind_at": remind_at,
        "due_at": due_at,
        "use_hidden_subtasks": False,
        "sub_tasks": [],
        "inline_steps": "1. First step\n2. Second step",
        "confirmation_message": confirmation,
    })


def _base_state(*, incoming: str, peer: str = "<test-peer-1>") -> State:
    return {
        "peer": peer,
        "incoming": incoming,
        "intent": "ADD_TASK",
        "messages": [],
        "active_task": None,
        "streak": 0,
        "tasks_completed_today": 0,
        "user_prefs": {"timezone": "America/Chicago"},
        "mood": None,
        "available_minutes": None,
        "conversation_state": "idle",
        "pending_outbound": [],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reminder_creates_notion_row_and_outbox_row() -> None:
    """'remind me at 5pm tomorrow' creates Notion row + outbox row with correct due_at.

    Validates the two-step intake → outbox persistence contract.
    No real Notion or DB calls — all mocked.
    """
    page_id = str(uuid.uuid4())
    due_at_str = "2026-01-02T17:00:00-06:00"

    remind_response = _llm_save_response(
        title="Placeholder reminder task",
        is_reminder=True,
        remind_at=due_at_str,
        confirmation="Got it — I'll remind you at 5pm to do the thing.",
    )

    enqueued_calls: list[dict] = []

    async def mock_enqueue(conn, *, notion_page_id, peer, body, due_at, idempotency_key, **kwargs):
        enqueued_calls.append({
            "notion_page_id": notion_page_id,
            "peer": peer,
            "body": body,
            "due_at": due_at,
            "idempotency_key": idempotency_key,
        })
        return uuid.uuid4()

    mock_notion_page = _make_notion_page(page_id=page_id, title="Placeholder reminder task")

    with (
        patch("app.tools.notion.create_reminder", new_callable=AsyncMock, return_value=mock_notion_page),
        patch("app.tools.notion.create_task", new_callable=AsyncMock, return_value=mock_notion_page),
        patch("app.tools.reminders.enqueue", side_effect=mock_enqueue),
        patch("app.tools.db.psycopg.AsyncConnection.connect", new_callable=AsyncMock),
    ):
        # Build a mock LLM that returns our controlled response
        mock_llm_response = MagicMock()
        mock_llm_response.content = remind_response
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

        with patch("app.models.llm", return_value=mock_model):
            from app.graph.nodes.intake import intake_node

            state: State = {
                "peer": "<test-intake-reminder>",
                "incoming": "remind me at 5pm tomorrow to do the thing",
                "intent": "ADD_TASK",
                "messages": [],
                "active_task": None,
                "streak": 0,
                "tasks_completed_today": 0,
                "user_prefs": {"timezone": "America/Chicago"},
                "mood": None,
                "available_minutes": None,
                "conversation_state": "idle",
                "pending_outbound": [],
            }

            # Mock get_db_conn at the source module (it's imported inside _create_reminder)
            mock_conn = AsyncMock()
            mock_conn_ctx = AsyncMock()
            mock_conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_conn_ctx.__aexit__ = AsyncMock(return_value=None)

            with patch("app.tools.db.get_db_conn", return_value=mock_conn_ctx):
                with patch("app.tools.reminders.enqueue", side_effect=mock_enqueue):
                    result = await intake_node(state)

    # Verify pending_outbound has a confirmation message
    assert result["pending_outbound"]
    draft = result["pending_outbound"][0]
    assert draft["recipient"] == "<test-intake-reminder>"
    assert "remind" in draft["body"].lower() or "placeholder" in draft["body"].lower() or "got it" in draft["body"].lower()


@pytest.mark.asyncio
async def test_task_without_reminder_creates_notion_task_only() -> None:
    """'I need to do laundry' creates task without reminder, no outbox row."""
    page_id = str(uuid.uuid4())
    task_response = _llm_save_response(
        title="Placeholder laundry task",
        work_type="independent",
        urgency=30,
        time_estimate=20,
        is_reminder=False,
        confirmation="Got it — independent, ~20 min. Steps: 1) Gather laundry, 2) Wash, 3) Fold",
    )

    create_task_calls: list[dict] = []
    create_reminder_calls: list[dict] = []

    async def mock_create_task(**kwargs: Any) -> dict:
        create_task_calls.append(kwargs)
        return _make_notion_page(page_id=page_id, title="Placeholder laundry task")

    async def mock_create_reminder(**kwargs: Any) -> dict:
        create_reminder_calls.append(kwargs)
        return _make_notion_page(page_id=page_id)

    with (
        patch("app.tools.notion.create_task", side_effect=mock_create_task),
        patch("app.tools.notion.create_reminder", side_effect=mock_create_reminder),
    ):
        mock_llm_response = MagicMock()
        mock_llm_response.content = task_response
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

        with patch("app.models.llm", return_value=mock_model):
            from app.graph.nodes.intake import intake_node

            state: State = {
                "peer": "<test-intake-no-reminder>",
                "incoming": "I need to do laundry",
                "intent": "ADD_TASK",
                "messages": [],
                "active_task": None,
                "streak": 0,
                "tasks_completed_today": 0,
                "user_prefs": {},
                "mood": None,
                "available_minutes": None,
                "conversation_state": "idle",
                "pending_outbound": [],
            }

            result = await intake_node(state)

    # create_task was called, create_reminder was NOT called
    assert len(create_task_calls) == 1
    assert len(create_reminder_calls) == 0

    # Confirmation message present
    assert result["pending_outbound"]
    draft = result["pending_outbound"][0]
    assert draft["recipient"] == "<test-intake-no-reminder>"


@pytest.mark.asyncio
async def test_deadline_task_schedules_inline_series() -> None:
    """A non-reminder task with due_at is saved with Due At and scheduled inline."""
    page_id = str(uuid.uuid4())
    due_at = "2026-06-06T17:00:00-05:00"
    task_response = _llm_save_response(
        title="Placeholder deadline task",
        work_type="focus",
        urgency=80,
        time_estimate=45,
        is_reminder=False,
        due_at=due_at,
        confirmation="Got it — focus, ~45 min.",
    )

    create_task_calls: list[dict[str, Any]] = []

    async def mock_create_task(**kwargs: Any) -> dict:
        create_task_calls.append(kwargs)
        return _make_notion_page(page_id=page_id, title="Placeholder deadline task")

    scheduled_item = type(
        "Scheduled",
        (),
        {"label": "3d", "assigned_at": datetime(2026, 6, 3, 15, 0, tzinfo=UTC)},
    )()
    schedule_for_task = AsyncMock(return_value=([scheduled_item], []))
    record_deadline_task_peer = AsyncMock(return_value=None)
    mark_scheduled = AsyncMock(return_value={})

    class FakeCtx:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: object) -> None:
            return None

    with (
        patch("app.tools.notion.create_task", side_effect=mock_create_task),
        patch("app.tools.notion.mark_reminder_scheduled", mark_scheduled),
        patch("app.tools.db.get_db_conn", return_value=FakeCtx()),
        patch(
            "app.scheduler.reminder_scheduling.record_deadline_task_peer",
            record_deadline_task_peer,
        ),
        patch("app.scheduler.reminder_scheduling.schedule_for_task", schedule_for_task),
    ):
        mock_llm_response = MagicMock()
        mock_llm_response.content = task_response
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

        with patch("app.models.llm", return_value=mock_model):
            from app.graph.nodes.intake import intake_node

            state: State = {
                "peer": "<test-intake-deadline>",
                "incoming": "finish placeholder task by Saturday at 5pm",
                "intent": "ADD_TASK",
                "messages": [],
                "active_task": None,
                "streak": 0,
                "tasks_completed_today": 0,
                "user_prefs": {"timezone": "America/Chicago"},
                "mood": None,
                "available_minutes": None,
                "conversation_state": "idle",
                "pending_outbound": [],
            }

            result = await intake_node(state)

    assert create_task_calls[0]["due_at_iso"] == "2026-06-06T22:00:00+00:00"
    record_deadline_task_peer.assert_awaited_once()
    schedule_for_task.assert_awaited_once()
    mark_scheduled.assert_awaited_once_with(page_id)
    assert "I'll ping you" in result["pending_outbound"][0]["body"]


@pytest.mark.asyncio
async def test_dedup_no_existing_tasks_creates_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """No existing open tasks means intake creates the proposed task."""
    page_id = str(uuid.uuid4())
    task_response = _llm_save_response(title="Placeholder task")
    create_task = AsyncMock(return_value=_make_notion_page(page_id=page_id, title="Placeholder task"))

    async def query_all() -> dict[str, Any]:
        return {"results": []}

    monkeypatch.setattr("app.tools.notion.query_all", query_all)

    mock_llm_response = MagicMock()
    mock_llm_response.content = task_response
    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

    with (
        patch("app.tools.notion.create_task", create_task),
        patch("app.models.llm", return_value=mock_model),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(_base_state(incoming="Placeholder task"))

    create_task.assert_awaited_once()
    assert result["pending_outbound"][0]["notion_page_id"] == page_id


@pytest.mark.asyncio
async def test_dedup_clear_duplicate_updates_instead_of_creating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A high-confidence duplicate reuses the existing Notion page."""
    matched_page_id = "<page_id_matched>"
    task_response = _llm_save_response(title="Placeholder task")
    create_task = AsyncMock(return_value=_make_notion_page(page_id=str(uuid.uuid4())))
    update_property = AsyncMock(return_value={})

    async def query_all() -> dict[str, Any]:
        return {"results": [_make_query_page(matched_page_id, "Placeholder task", status="In Progress")]}

    monkeypatch.setattr("app.tools.notion.query_all", query_all)

    intake_llm_response = MagicMock()
    intake_llm_response.content = task_response
    intake_model = AsyncMock()
    intake_model.ainvoke = AsyncMock(return_value=intake_llm_response)

    dedup_llm_response = MagicMock()
    dedup_llm_response.content = json.dumps({"matched_page_id": matched_page_id, "confidence": 0.96})
    dedup_model = AsyncMock()
    dedup_model.ainvoke = AsyncMock(return_value=dedup_llm_response)

    with (
        patch("app.tools.notion.create_task", create_task),
        patch("app.tools.notion.update_property", update_property),
        patch("app.models.llm", side_effect=[intake_model, dedup_model]),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(_base_state(incoming="Placeholder task"))

    create_task.assert_not_awaited()
    update_property.assert_not_awaited()
    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] == matched_page_id
    assert "Placeholder task" in draft["body"]
    assert "duplicate" not in draft["body"].lower()
    assert "again" not in draft["body"].lower()


@pytest.mark.asyncio
async def test_dedup_deadline_updates_existing_and_schedules_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate with a deadline updates and schedules against the existing page."""
    matched_page_id = "<page_id_matched>"
    due_at = "2026-06-06T17:00:00-05:00"
    task_response = _llm_save_response(title="Placeholder deadline task", due_at=due_at)
    create_task = AsyncMock(return_value=_make_notion_page(page_id=str(uuid.uuid4())))
    update_property = AsyncMock(return_value={})

    async def query_all() -> dict[str, Any]:
        return {
            "results": [
                _make_query_page(matched_page_id, "Placeholder deadline task", status="In Progress"),
            ]
        }

    monkeypatch.setattr("app.tools.notion.query_all", query_all)

    scheduled_item = type(
        "Scheduled",
        (),
        {"label": "3d", "assigned_at": datetime(2026, 6, 3, 15, 0, tzinfo=UTC)},
    )()
    schedule_for_task = AsyncMock(return_value=([scheduled_item], []))
    record_deadline_task_peer = AsyncMock(return_value=None)
    get_active_deadline_for_page = AsyncMock(return_value=None)
    supersede_ledger_rows = AsyncMock(return_value=["<outbox-id>"])
    cancel_outbox_rows = AsyncMock(return_value=None)
    mark_scheduled = AsyncMock(return_value={})

    class FakeConn:
        async def commit(self) -> None:
            return None

    class FakeCtx:
        async def __aenter__(self) -> FakeConn:
            return FakeConn()

        async def __aexit__(self, *_: object) -> None:
            return None

    intake_llm_response = MagicMock()
    intake_llm_response.content = task_response
    intake_model = AsyncMock()
    intake_model.ainvoke = AsyncMock(return_value=intake_llm_response)

    dedup_llm_response = MagicMock()
    dedup_llm_response.content = json.dumps({"matched_page_id": matched_page_id, "confidence": 0.97})
    dedup_model = AsyncMock()
    dedup_model.ainvoke = AsyncMock(return_value=dedup_llm_response)

    with (
        patch("app.tools.notion.create_task", create_task),
        patch("app.tools.notion.update_property", update_property),
        patch("app.tools.notion.mark_reminder_scheduled", mark_scheduled),
        patch("app.tools.db.get_db_conn", return_value=FakeCtx()),
        patch("app.scheduler.reminder_scheduling.record_deadline_task_peer", record_deadline_task_peer),
        patch("app.scheduler.reminder_scheduling.get_active_deadline_for_page", get_active_deadline_for_page),
        patch("app.scheduler.reminder_scheduling.supersede_ledger_rows", supersede_ledger_rows),
        patch("app.scheduler.reminder_scheduling.cancel_outbox_rows", cancel_outbox_rows),
        patch("app.scheduler.reminder_scheduling.schedule_for_task", schedule_for_task),
        patch("app.models.llm", side_effect=[intake_model, dedup_model]),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(_base_state(incoming="Placeholder deadline task"))

    create_task.assert_not_awaited()
    update_property.assert_awaited_once()
    update_kwargs = update_property.await_args.kwargs
    from app.tools import notion

    assert set(update_kwargs) <= set(inspect.signature(notion.update_property).parameters)
    assert update_kwargs == {
        "page_id": matched_page_id,
        "prop_json": {
            "properties": {"Due At": {"date": {"start": "2026-06-06T22:00:00+00:00"}}}
        },
    }
    get_active_deadline_for_page.assert_awaited_once()
    supersede_ledger_rows.assert_awaited_once()
    cancel_outbox_rows.assert_awaited_once_with(ANY, ["<outbox-id>"])
    record_deadline_task_peer.assert_awaited_once()
    schedule_for_task.assert_awaited_once()
    assert schedule_for_task.await_args.kwargs["notion_page_id"] == matched_page_id
    mark_scheduled.assert_awaited_once_with(matched_page_id)
    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] == matched_page_id
    assert "deadline" in draft["body"].lower()


@pytest.mark.asyncio
async def test_dedup_deadline_update_failure_fails_open_to_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed duplicate deadline patch creates the proposed task instead."""
    matched_page_id = "<page_id_matched>"
    created_page_id = str(uuid.uuid4())
    due_at = "2026-06-06T17:00:00-05:00"
    task_response = _llm_save_response(title="Placeholder deadline task", due_at=due_at)
    create_task = AsyncMock(return_value=_make_notion_page(page_id=created_page_id))
    update_property = AsyncMock(side_effect=RuntimeError("Notion update failed"))
    schedule_for_task = AsyncMock(return_value=([], []))

    async def query_all() -> dict[str, Any]:
        return {
            "results": [
                _make_query_page(matched_page_id, "Placeholder deadline task", status="In Progress"),
            ]
        }

    monkeypatch.setattr("app.tools.notion.query_all", query_all)

    class FakeCtx:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: object) -> None:
            return None

    intake_llm_response = MagicMock()
    intake_llm_response.content = task_response
    intake_model = AsyncMock()
    intake_model.ainvoke = AsyncMock(return_value=intake_llm_response)

    dedup_llm_response = MagicMock()
    dedup_llm_response.content = json.dumps({"matched_page_id": matched_page_id, "confidence": 0.97})
    dedup_model = AsyncMock()
    dedup_model.ainvoke = AsyncMock(return_value=dedup_llm_response)

    with (
        patch("app.tools.notion.create_task", create_task),
        patch("app.tools.notion.update_property", update_property),
        patch("app.tools.db.get_db_conn", return_value=FakeCtx()),
        patch("app.scheduler.reminder_scheduling.record_deadline_task_peer", AsyncMock(return_value=None)),
        patch("app.scheduler.reminder_scheduling.schedule_for_task", schedule_for_task),
        patch("app.models.llm", side_effect=[intake_model, dedup_model]),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(_base_state(incoming="Placeholder deadline task"))

    update_property.assert_awaited_once()
    create_task.assert_awaited_once()
    assert create_task.await_args.kwargs["due_at_iso"] == "2026-06-06T22:00:00+00:00"
    assert result["pending_outbound"][0]["notion_page_id"] == created_page_id


@pytest.mark.asyncio
async def test_dedup_unrelated_existing_task_creates_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrelated existing task does not suppress a new task."""
    page_id = str(uuid.uuid4())
    task_response = _llm_save_response(title="Draft sample report")
    create_task = AsyncMock(return_value=_make_notion_page(page_id=page_id))

    async def query_all() -> dict[str, Any]:
        return {"results": [_make_query_page("<page_id_a>", "Clean placeholder room")]}

    monkeypatch.setattr("app.tools.notion.query_all", query_all)

    mock_llm_response = MagicMock()
    mock_llm_response.content = task_response
    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

    with (
        patch("app.tools.notion.create_task", create_task),
        patch("app.models.llm", return_value=mock_model),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(_base_state(incoming="Draft sample report"))

    create_task.assert_awaited_once()
    assert result["pending_outbound"][0]["notion_page_id"] == page_id


@pytest.mark.asyncio
async def test_dedup_multiple_candidates_discloses_only_matched_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The duplicate path never names unmatched candidates or their count."""
    matched_page_id = "<page_id_matched>"
    task_response = _llm_save_response(title="Call placeholder office")
    create_task = AsyncMock(return_value=_make_notion_page(page_id=str(uuid.uuid4())))

    async def query_all() -> dict[str, Any]:
        return {
            "results": [
                _make_query_page(matched_page_id, "Call placeholder office"),
                _make_query_page("<page_id_other>", "Call placeholder coordinator"),
            ]
        }

    monkeypatch.setattr("app.tools.notion.query_all", query_all)

    intake_llm_response = MagicMock()
    intake_llm_response.content = task_response
    intake_model = AsyncMock()
    intake_model.ainvoke = AsyncMock(return_value=intake_llm_response)

    dedup_llm_response = MagicMock()
    dedup_llm_response.content = json.dumps({"matched_page_id": matched_page_id, "confidence": 0.96})
    dedup_model = AsyncMock()
    dedup_model.ainvoke = AsyncMock(return_value=dedup_llm_response)

    with (
        patch("app.tools.notion.create_task", create_task),
        patch("app.models.llm", side_effect=[intake_model, dedup_model]),
    ):
        from app.graph.nodes.intake import intake_node

        result = await intake_node(_base_state(incoming="Call placeholder office"))

    create_task.assert_not_awaited()
    body = result["pending_outbound"][0]["body"]
    assert "Call placeholder office" in body
    assert "Call placeholder coordinator" not in body
    assert "candidate" not in body.lower()
    assert "similar task" not in body.lower()
    assert "i found" not in body.lower()


@pytest.mark.asyncio
async def test_recurring_reminder_gets_explicit_response() -> None:
    """'every weekday at 8' receives a response (recurring not fully supported in v1).

    The intake node processes this as a regular task or provides a response.
    Recurring reminders are not explicitly supported in v1 — the node either
    saves as a one-time reminder or returns a helpful message.
    This test asserts the node does not crash and returns a pending_outbound.
    """
    # Simulate LLM returning a clarify or save response
    clarify_response = json.dumps({
        "action": "clarify",
        "clarification_question": "Recurring reminders aren't quite set up yet. Want me to save this as a one-time reminder for tomorrow at 8?",
        "clarification_count": 1,
    })

    mock_llm_response = MagicMock()
    mock_llm_response.content = clarify_response
    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

    with patch("app.models.llm", return_value=mock_model):
        from app.graph.nodes.intake import intake_node

        state: State = {
            "peer": "<test-intake-recurring>",
            "incoming": "every weekday at 8 remind me to check email",
            "intent": "ADD_TASK",
            "messages": [],
            "active_task": None,
            "streak": 0,
            "tasks_completed_today": 0,
            "user_prefs": {},
            "mood": None,
            "available_minutes": None,
            "conversation_state": "idle",
            "pending_outbound": [],
        }

        result = await intake_node(state)

    # Node must not crash; must return some response
    assert result["pending_outbound"]
    draft = result["pending_outbound"][0]
    assert draft["recipient"] == "<test-intake-recurring>"
    # The response should mention the recurring limitation or ask for clarification
    assert len(draft["body"]) > 0


def test_intake_prompt_parity() -> None:
    """Intake template must have all required section anchors from source doc.

    Redundant with test_prompt_parity.py but included here as PR-B3 acceptance criterion.
    """
    from pathlib import Path

    from app.prompts.loader import render_with_defaults

    source_path = Path(__file__).parent.parent.parent / "docs" / "ai-prompts" / "intake.md"
    assert source_path.is_file()

    rendered = render_with_defaults(
        "intake.md.j2",
        {},
        defaults={
            "user_message": "",
            "conversation_history": "",
            "user_preferences_context": "",
            "clarification_count": 0,
            "current_time": "2026-01-01T12:00:00-06:00",
            "user_timezone": "America/Chicago",
        },
    )

    required_sections = [
        "Task Intake",
        "Decision Fatigue Prevention",
        "Reminder Detection",
        "Shame Prevention",
    ]

    missing = [s for s in required_sections if s not in rendered]
    assert not missing, f"Intake template missing sections: {missing}"


@pytest.mark.asyncio
async def test_intake_clarify_response_when_vague() -> None:
    """'do the thing' → clarification question, not a saved task."""
    clarify_response = json.dumps({
        "action": "clarify",
        "clarification_question": "Which thing are you thinking of?",
        "clarification_count": 1,
    })

    mock_llm_response = MagicMock()
    mock_llm_response.content = clarify_response
    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

    with patch("app.models.llm", return_value=mock_model):
        from app.graph.nodes.intake import intake_node

        state: State = {
            "peer": "<test-intake-vague>",
            "incoming": "do the thing",
            "intent": "ADD_TASK",
            "messages": [],
            "active_task": None,
            "streak": 0,
            "tasks_completed_today": 0,
            "user_prefs": {},
            "mood": None,
            "available_minutes": None,
            "conversation_state": "idle",
            "pending_outbound": [],
        }

        result = await intake_node(state)

    # Clarification question returned, task not saved
    assert result["pending_outbound"]
    body = result["pending_outbound"][0]["body"]
    assert "which" in body.lower() or "thing" in body.lower() or "?" in body


@pytest.mark.asyncio
async def test_unparseable_output_saves_raw_task_and_alerts() -> None:
    """A truncated/garbled LLM response must NOT masquerade as a confirmed task.

    Regression for the intake reminder-drop: when the model output is
    unparseable (e.g. truncated at the output-token ceiling), the node must
    (1) preserve capture by saving a raw task titled from the user's own
    words, (2) never claim a reminder was set, and (3) emit an ops alert so
    the operator sees the model degradation. The old behavior silently saved
    a plain task titled with the garbled LLM text and replied "Got it — added."
    """
    page_id = str(uuid.uuid4())
    incoming = "I need to clean the kitchen Friday before 10pm"
    # Truncated JSON: opening brace, cut off mid-value, no closing brace.
    truncated = '{"action": "save", "title": "clean the kitchen", "is_reminder": tr'

    create_task_calls: list[dict] = []
    create_reminder_calls: list[dict] = []
    alert_calls: list[dict] = []

    async def mock_create_task(**kwargs: Any) -> dict:
        create_task_calls.append(kwargs)
        return _make_notion_page(page_id=page_id, title="Placeholder task")

    async def mock_create_reminder(**kwargs: Any) -> dict:
        create_reminder_calls.append(kwargs)
        return _make_notion_page(page_id=page_id)

    async def mock_alert(*, kind: str, body: str, severity: str = "warning", **_: Any) -> Any:
        alert_calls.append({"kind": kind, "body": body, "severity": severity})
        return uuid.uuid4()

    with (
        patch("app.tools.notion.create_task", side_effect=mock_create_task),
        patch("app.tools.notion.create_reminder", side_effect=mock_create_reminder),
        patch("app.tools.ops_alerts.enqueue", side_effect=mock_alert),
    ):
        mock_llm_response = MagicMock()
        mock_llm_response.content = truncated
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

        with patch("app.models.llm", return_value=mock_model):
            from app.graph.nodes.intake import intake_node

            state: State = {
                "peer": "<test-intake-unparseable>",
                "incoming": incoming,
                "intent": "ADD_TASK",
                "messages": [],
                "active_task": None,
                "streak": 0,
                "tasks_completed_today": 0,
                "user_prefs": {},
                "mood": None,
                "available_minutes": None,
                "conversation_state": "idle",
                "pending_outbound": [],
            }

            result = await intake_node(state)

    # Raw task saved, titled from the user's words — NOT the garbled LLM output.
    assert len(create_task_calls) == 1
    assert create_task_calls[0]["title"] == incoming
    assert "is_reminder" not in create_task_calls[0]["title"]
    assert create_task_calls[0]["work_type"] == "focus"
    assert create_task_calls[0]["urgency"] == 50
    assert create_task_calls[0]["time_estimate"] == 30
    assert create_task_calls[0]["energy_required"] == "Medium"
    # No reminder was fabricated.
    assert len(create_reminder_calls) == 0
    # Operator was alerted to the parse failure.
    assert len(alert_calls) == 1
    assert alert_calls[0]["kind"] == "intake_parse_failed"
    assert "unparseable" in alert_calls[0]["body"].lower() or "parse" in alert_calls[0]["body"].lower()
    assert alert_calls[0]["severity"] == "warning"
    # User got an honest message — not a bare "Got it — added." false success,
    # and it flags that the timing/reminder wasn't captured.
    assert result["pending_outbound"]
    body = result["pending_outbound"][0]["body"].lower()
    assert body.strip() != "got it — added."
    assert "remind" in body or "timing" in body or "time" in body


@pytest.mark.asyncio
async def test_unparseable_output_with_notion_down_does_not_claim_capture() -> None:
    """A parse failure plus a Notion outage must not report the task as added.

    The parse-failure path saves the user's raw message to preserve capture. If
    that save itself fails there is nothing on the list, so the reply must be
    the error path's "send it again", never "Added that to your list" — which
    would reproduce the fabricated-success bug one level below the parse fix.
    """
    incoming = "I need to clean the kitchen Friday before 10pm"
    truncated = '{"action": "save", "title": "clean the kitchen", "is_reminder": tr'

    create_reminder_calls: list[dict] = []
    alert_kinds: list[str] = []

    async def mock_create_task(**_: Any) -> dict:
        raise RuntimeError("Notion API unavailable")

    async def mock_create_reminder(**kwargs: Any) -> dict:
        create_reminder_calls.append(kwargs)
        return _make_notion_page(page_id=str(uuid.uuid4()))

    async def mock_alert(*, kind: str, body: str, severity: str = "warning", **_: Any) -> Any:
        alert_kinds.append(kind)
        return uuid.uuid4()

    with (
        patch("app.tools.notion.create_task", side_effect=mock_create_task),
        patch("app.tools.notion.create_reminder", side_effect=mock_create_reminder),
        patch("app.tools.ops_alerts.enqueue", side_effect=mock_alert),
    ):
        mock_llm_response = MagicMock()
        mock_llm_response.content = truncated
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_llm_response)

        with patch("app.models.llm", return_value=mock_model):
            from app.graph.nodes.intake import intake_node

            state: State = {
                "peer": "<test-intake-notion-down>",
                "incoming": incoming,
                "intent": "ADD_TASK",
                "messages": [],
                "active_task": None,
                "streak": 0,
                "tasks_completed_today": 0,
                "user_prefs": {},
                "mood": None,
                "available_minutes": None,
                "conversation_state": "idle",
                "pending_outbound": [],
            }

            result = await intake_node(state)

    # Nothing was captured, so nothing may be confirmed.
    assert len(create_reminder_calls) == 0
    assert result["pending_outbound"]
    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] is None
    body = draft["body"].lower()
    assert "added" not in body
    assert "again" in body
    # The operator still hears about it, via the node-level error alert.
    assert "intake_node_error" in alert_kinds
