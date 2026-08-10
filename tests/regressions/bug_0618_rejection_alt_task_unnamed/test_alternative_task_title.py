"""Regression for rejection alternatives that are selected but unnamed."""
from __future__ import annotations

import json
from typing import Any

import pytest


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeModel:
    def __init__(self, content: str) -> None:
        self._content = content

    async def ainvoke(self, messages: object) -> _FakeResponse:
        return _FakeResponse(self._content)


def _notion_task(page_id: str, title: str, minutes: int) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Work Type": {"select": {"name": "admin"}},
            "Time Estimate (min)": {"number": minutes},
        },
    }


@pytest.mark.asyncio
async def test_rejection_node_names_selected_alternative_without_task_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid alternative_task_id cannot produce a body that omits the task title."""
    from app import models as models_module
    from app.graph.nodes.rejection import rejection_node
    from app.tools import notion

    alternative_id = "<page-id-alternative>"
    alternative_title = "Placeholder alternative task"

    async def fake_query_pending() -> dict[str, Any]:
        return {
            "results": [
                _notion_task("<page-id-rejected>", "Placeholder rejected task", 45),
                _notion_task(alternative_id, alternative_title, 30),
            ]
        }

    async def fake_update_property(page_id: str, prop_json: dict[str, Any]) -> dict[str, Any]:
        return {"id": page_id, "properties": prop_json["properties"]}

    model_response = json.dumps(
        {
            "rejection_category": "timing",
            "task_update": {
                "rejection_count_increment": 1,
                "rejection_note": "[timestamp] too long",
            },
            "alternative_task_id": alternative_id,
            "user_message": (
                "No problem — that helps me learn what works for you. "
                "Since that one was a bit heavy, how about this 30-minute task instead?"
            ),
        }
    )

    monkeypatch.setattr(notion, "query_pending", fake_query_pending)
    monkeypatch.setattr(notion, "update_property", fake_update_property)
    monkeypatch.setattr(models_module, "llm", lambda tier, **kwargs: _FakeModel(model_response))

    result = await rejection_node(
        {
            "peer": "<recipient>",
            "incoming": "Too much right now",
            "intent": "REJECT",
            "messages": [],
            "active_task": {
                "page_id": "<page-id-rejected>",
                "title": "Placeholder rejected task",
                "status": "In Progress",
                "work_type": "focus",
                "urgency": 50,
                "time_estimate": 45,
                "energy_required": "Medium",
                "rejection_count": 0,
            },
            "streak": 0,
            "tasks_completed_today": 0,
            "user_prefs": {},
            "mood": None,
            "available_minutes": 30,
            "conversation_state": "active",
            "pending_outbound": [],
        }
    )

    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] == alternative_id
    assert alternative_title in draft["body"]


@pytest.mark.asyncio
async def test_rejection_node_replaces_task_token_with_selected_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prompt convention lets code own the exact selected task title."""
    from app import models as models_module
    from app.graph.nodes.rejection import rejection_node
    from app.tools import notion

    alternative_id = "<page-id-alternative>"
    alternative_title = "Placeholder short task"

    async def fake_query_pending() -> dict[str, Any]:
        return {"results": [_notion_task(alternative_id, alternative_title, 10)]}

    async def fake_update_property(page_id: str, prop_json: dict[str, Any]) -> dict[str, Any]:
        return {"id": page_id, "properties": prop_json["properties"]}

    model_response = json.dumps(
        {
            "rejection_category": "timing",
            "task_update": {
                "rejection_count_increment": 1,
                "rejection_note": "[timestamp] too long",
            },
            "alternative_task_id": alternative_id,
            "user_message": "Got it — that one's too long right now. How about {task}?",
        }
    )

    monkeypatch.setattr(notion, "query_pending", fake_query_pending)
    monkeypatch.setattr(notion, "update_property", fake_update_property)
    monkeypatch.setattr(models_module, "llm", lambda tier, **kwargs: _FakeModel(model_response))

    result = await rejection_node(
        {
            "peer": "<recipient>",
            "incoming": "Too much right now",
            "intent": "REJECT",
            "messages": [],
            "active_task": {
                "page_id": "<page-id-rejected>",
                "title": "Placeholder rejected task",
                "status": "In Progress",
                "work_type": "focus",
                "urgency": 50,
                "time_estimate": 45,
                "energy_required": "Medium",
                "rejection_count": 0,
            },
            "streak": 0,
            "tasks_completed_today": 0,
            "user_prefs": {},
            "mood": None,
            "available_minutes": 30,
            "conversation_state": "active",
            "pending_outbound": [],
        }
    )

    draft = result["pending_outbound"][0]
    assert draft["body"] == f"Got it — that one's too long right now. How about {alternative_title}?"
