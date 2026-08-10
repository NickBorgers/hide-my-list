"""Unit coverage for intake near-duplicate detection."""
from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.graph.nodes.intake import shortlist_duplicate_candidates
from app.graph.state import State


def test_shortlist_duplicate_candidates_exact_match() -> None:
    candidates = shortlist_duplicate_candidates(
        "Placeholder task",
        [{"id": "<page_id_a>", "title": "Placeholder task"}],
    )

    assert [candidate.page_id for candidate in candidates] == ["<page_id_a>"]
    assert candidates[0].score == 1.0


def test_shortlist_duplicate_candidates_near_match() -> None:
    candidates = shortlist_duplicate_candidates(
        "Call placeholder office Friday",
        [{"id": "<page_id_a>", "title": "Call the placeholder office"}],
    )

    assert [candidate.page_id for candidate in candidates] == ["<page_id_a>"]


def test_shortlist_duplicate_candidates_unrelated_titles() -> None:
    candidates = shortlist_duplicate_candidates(
        "Draft sample report",
        [{"id": "<page_id_a>", "title": "Clean placeholder room"}],
    )

    assert candidates == []


def test_shortlist_duplicate_candidates_empty_task_list() -> None:
    assert shortlist_duplicate_candidates("Placeholder task", []) == []


def test_shortlist_duplicate_candidates_unicode_and_punctuation_normalization() -> None:
    candidates = shortlist_duplicate_candidates(
        "Email PLACEHOLDER—today!",
        [{"id": "<page_id_a>", "title": "email placeholder today"}],
    )

    assert [candidate.page_id for candidate in candidates] == ["<page_id_a>"]


def test_query_stopwords_strip_the_query_side_only() -> None:
    """Callers matching a whole message drop filler the title still needs.

    "out" carries no signal in "knocked that out" and all of it in "Take out
    the placeholder" — so it comes off the query and stays on the title.
    """
    tasks = [{"id": "<page_id_a>", "title": "Take out the placeholder"}]

    without = shortlist_duplicate_candidates("Knocked out the placeholder", tasks)
    with_stopwords = shortlist_duplicate_candidates(
        "Knocked out the placeholder",
        tasks,
        query_stopwords=frozenset({"knocked", "out"}),
    )

    assert without[0].score > 0
    # Dropping the filler leaves {placeholder} against {take, out, placeholder}.
    assert with_stopwords[0].score == pytest.approx(0.5)


def test_query_stopwords_can_empty_the_query() -> None:
    """A message made entirely of filler shortlists nothing."""
    assert (
        shortlist_duplicate_candidates(
            "knocked it out",
            [{"id": "<page_id_a>", "title": "Take out the placeholder"}],
            query_stopwords=frozenset({"knocked", "out"}),
        )
        == []
    )


def test_query_stopwords_default_to_empty() -> None:
    """Intake passes no query stopwords; its scoring is unchanged."""
    tasks = [{"id": "<page_id_a>", "title": "Take out the placeholder"}]
    assert shortlist_duplicate_candidates("Take out the placeholder", tasks) == (
        shortlist_duplicate_candidates(
            "Take out the placeholder", tasks, query_stopwords=frozenset()
        )
    )


@pytest.mark.asyncio
async def test_dedup_notion_error_fails_open_to_task_creation() -> None:
    page_id = str(uuid.uuid4())
    create_task = AsyncMock(return_value={"id": page_id})

    async def raise_query_all() -> dict[str, Any]:
        raise RuntimeError("Notion unavailable")

    result = await _run_intake_with_dedup(
        create_task=create_task,
        query_all=raise_query_all,
    )

    create_task.assert_awaited_once()
    assert result["pending_outbound"][0]["notion_page_id"] == page_id


@pytest.mark.asyncio
async def test_dedup_garbage_llm_response_fails_open_to_task_creation() -> None:
    page_id = str(uuid.uuid4())
    create_task = AsyncMock(return_value={"id": page_id})

    async def query_all() -> dict[str, Any]:
        return {
            "results": [
                _notion_page("<page_id_a>", "Placeholder task", status="In Progress"),
            ]
        }

    result = await _run_intake_with_dedup(
        create_task=create_task,
        query_all=query_all,
        dedup_response="not json",
    )

    create_task.assert_awaited_once()
    assert result["pending_outbound"][0]["notion_page_id"] == page_id


async def _run_intake_with_dedup(
    *,
    create_task: AsyncMock,
    query_all: Any,
    dedup_response: str | None = None,
) -> dict[str, Any]:
    intake_response = json.dumps({
        "action": "save",
        "title": "Placeholder task",
        "work_type": "focus",
        "urgency": 50,
        "time_estimate_minutes": 30,
        "energy_required": "Medium",
        "is_reminder": False,
        "remind_at": None,
        "due_at": None,
        "use_hidden_subtasks": False,
        "sub_tasks": [],
        "inline_steps": "1. Placeholder step",
        "confirmation_message": "Got it — focus, ~30 min.",
    })

    intake_llm_response = MagicMock()
    intake_llm_response.content = intake_response
    intake_model = AsyncMock()
    intake_model.ainvoke = AsyncMock(return_value=intake_llm_response)

    llm_models: list[Any] = [intake_model]
    if dedup_response is not None:
        dedup_llm_response = MagicMock()
        dedup_llm_response.content = dedup_response
        dedup_model = AsyncMock()
        dedup_model.ainvoke = AsyncMock(return_value=dedup_llm_response)
        llm_models.append(dedup_model)

    with (
        patch("app.models.llm", side_effect=llm_models),
        patch("app.tools.notion.query_all", side_effect=query_all),
        patch("app.tools.notion.create_task", create_task),
    ):
        from app.graph.nodes.intake import intake_node

        state: State = {
            "peer": "<test-peer-1>",
            "incoming": "Placeholder task",
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

        return await intake_node(state)


def _notion_page(page_id: str, title: str, *, status: str = "Pending") -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": status}},
            "Is Reminder": {"checkbox": False},
        },
    }
