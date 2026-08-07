"""Regression: COMPLETE resolves the task named in the message (bug #655).

The production failure had both context sources empty — a stale `active_task`
outside its 24h TTL and no unresolved `recent_outbound` row — while the message
itself named the finished task. Resolution never read the message, so the node
returned a fully null target and asked which task was meant.

These tests hold the two halves of the fix together: the named task must
resolve, and resolving it must not weaken the guards around the destructive
Notion write it triggers.
"""
from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.graph.nodes import complete as complete_module
from app.graph.state import State
from app.tools import notion, rewards


def _assert_write_kwargs_shape(update_status: AsyncMock, page_id: str) -> None:
    """Validate the Notion write against the real signature.

    complete_node wraps its whole body in `except Exception`, so a call with a
    drifted signature would be swallowed and reported to the user as success.
    Bug class 10: silent degradation behind intentional exception-swallowing.
    """
    update_status.assert_awaited_once()
    bound = inspect.signature(notion.update_status).bind(
        *update_status.await_args.args, **update_status.await_args.kwargs
    )
    bound.apply_defaults()
    assert bound.arguments["page_id"] == page_id
    assert bound.arguments["new_status"] == "Completed"


def _assert_reward_kwargs_shape(reward_mock: AsyncMock, page_id: str) -> None:
    """Every kwarg must be a real parameter, and the required set must be complete."""
    reward_mock.assert_awaited_once()
    kwargs = reward_mock.await_args.kwargs
    assert set(kwargs) <= set(inspect.signature(rewards.maybe_reward).parameters)
    assert set(kwargs) == {
        "peer",
        "task_title",
        "notion_page_id",
        "streak",
        "work_type",
        "energy_required",
    }
    assert kwargs["notion_page_id"] == page_id


def _notion_page(page_id: str, title: str, status: str = "Pending") -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": status}},
            "Is Reminder": {"checkbox": False},
        },
    }


def _stale_active_task(title: str, page_id: str) -> dict[str, Any]:
    """An active task outside the 24h TTL — the production shape."""
    return {
        "page_id": page_id,
        "title": title,
        "status": "In Progress",
        "selected_at": (datetime.now(UTC) - timedelta(hours=48)).isoformat(),
        "work_type": "Physical",
        "energy_required": "Low",
    }


def _model(content: str) -> AsyncMock:
    response = MagicMock()
    response.content = content
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    return model


def _state(incoming: str, active_task: dict[str, Any] | None = None) -> State:
    return {  # type: ignore[return-value]
        "peer": "<test-peer>",
        "incoming": incoming,
        "intent": "COMPLETE",
        "messages": [],
        "active_task": active_task,
        "streak": 1,
        "tasks_completed_today": 0,
        "user_prefs": {},
        "mood": None,
        "available_minutes": None,
        "conversation_state": "idle",
        "pending_outbound": [],
    }


@pytest.mark.asyncio
async def test_named_task_resolves_when_context_is_empty() -> None:
    """Stale active task + no reminder + a task named in the message."""
    update_status = AsyncMock()
    reward_mock = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    query_all = AsyncMock(return_value={"results": [
        _notion_page("<page_A>", "Wash the dishes"),
        _notion_page("<page_B>", "Book the dentist appointment"),
    ]})

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", query_all),
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", return_value=_model(
            json.dumps({"matched_page_id": "<page_A>", "confidence": 0.95})
        )),
    ):
        result = await complete_module.complete_node(
            _state(
                "finally washed the dishes",
                _stale_active_task("Book the dentist appointment", "<page_B>"),
            )
        )

    _assert_write_kwargs_shape(update_status, "<page_A>")
    _assert_reward_kwargs_shape(reward_mock, "<page_A>")
    assert result["pending_outbound"][0]["notion_page_id"] == "<page_A>"
    assert "which task" not in result["pending_outbound"][0]["body"].lower()


@pytest.mark.asyncio
async def test_named_task_outranks_a_live_active_task_on_another_page() -> None:
    """Naming task B while task A is live must not complete A."""
    update_status = AsyncMock()
    reward_mock = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    query_all = AsyncMock(return_value={"results": [_notion_page("<page_B>", "Wash the dishes")]})
    live_active = {
        "page_id": "<page_A>",
        "title": "Fold the laundry",
        "selected_at": datetime.now(UTC).isoformat(),
        "work_type": "Physical",
        "energy_required": "Low",
    }

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", query_all),
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", return_value=_model(
            json.dumps({"matched_page_id": "<page_B>", "confidence": 0.95})
        )),
    ):
        await complete_module.complete_node(_state("done with the dishes", live_active))

    _assert_write_kwargs_shape(update_status, "<page_B>")
    _assert_reward_kwargs_shape(reward_mock, "<page_B>")


@pytest.mark.asyncio
async def test_a_task_the_user_still_has_to_do_is_not_completed() -> None:
    """"done, now I need to call mom" overlaps "Call mom" on every word.

    The shortlist cannot separate "I finished this" from "this is what I do
    next"; only the model reading the whole sentence can. A null match has to
    leave the task open.
    """
    update_status = AsyncMock()
    reward_mock = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    query_all = AsyncMock(return_value={"results": [_notion_page("<page_A>", "Call mom")]})

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", query_all),
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", return_value=_model(
            json.dumps({"matched_page_id": None, "confidence": 0.0})
        )),
    ):
        result = await complete_module.complete_node(_state("done, now I need to call mom"))

    update_status.assert_not_awaited()
    reward_mock.assert_not_awaited()
    assert result["pending_outbound"][0]["notion_page_id"] is None


@pytest.mark.asyncio
async def test_sub_threshold_confidence_does_not_write() -> None:
    """0.85 clears intake's bar and not this one — the write is destructive."""
    update_status = AsyncMock()
    reward_mock = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    query_all = AsyncMock(return_value={"results": [_notion_page("<page_A>", "Wash the dishes")]})

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", query_all),
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", return_value=_model(
            json.dumps({"matched_page_id": "<page_A>", "confidence": 0.85})
        )),
    ):
        result = await complete_module.complete_node(_state("done with the dishes"))

    update_status.assert_not_awaited()
    reward_mock.assert_not_awaited()
    assert result["pending_outbound"][0]["notion_page_id"] is None


@pytest.mark.asyncio
async def test_a_notion_failure_while_matching_falls_through_to_context() -> None:
    """The lookup may add a resolution, never subtract one."""
    update_status = AsyncMock()
    reward_mock = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    live_active = {
        "page_id": "<page_A>",
        "title": "Wash the dishes",
        "selected_at": datetime.now(UTC).isoformat(),
        "work_type": "Physical",
        "energy_required": "Low",
    }

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", AsyncMock(side_effect=RuntimeError("Notion down"))),
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
    ):
        result = await complete_module.complete_node(
            _state("done with the dishes", live_active)
        )

    _assert_write_kwargs_shape(update_status, "<page_A>")
    assert result["pending_outbound"][0]["notion_page_id"] == "<page_A>"


@pytest.mark.asyncio
async def test_a_bare_completion_reads_neither_notion_nor_the_model() -> None:
    """"done!" must stay on the pre-existing path — no lookup, no model call."""
    query_all = AsyncMock()
    llm_factory = MagicMock()
    live_active = {
        "page_id": "<page_A>",
        "title": "Wash the dishes",
        "selected_at": datetime.now(UTC).isoformat(),
        "work_type": "Physical",
        "energy_required": "Low",
    }

    with (
        patch("app.tools.notion.update_status", new_callable=AsyncMock),
        patch("app.tools.notion.query_all", query_all),
        patch(
            "app.tools.rewards.maybe_reward",
            new_callable=AsyncMock,
            return_value={"text": "Nice work!", "attachment_path": None},
        ),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", llm_factory),
    ):
        await complete_module.complete_node(_state("done!", live_active))

    query_all.assert_not_awaited()
    llm_factory.assert_not_called()
