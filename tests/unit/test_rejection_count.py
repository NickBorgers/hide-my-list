"""Tests for preserving rejection count across selection and rejection."""
from __future__ import annotations

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


def _notion_task(page_id: str, rejection_count: int) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": "Placeholder task"}]},
            "Work Type": {"select": {"name": "focus"}},
            "Urgency": {"number": 50},
            "Time Estimate (min)": {"number": 30},
            "Energy Required": {"select": {"name": "Medium"}},
            "Rejection Count": {"number": rejection_count},
        },
    }


@pytest.mark.asyncio
async def test_selection_node_preserves_rejection_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selected active_task carries current Notion Rejection Count."""
    from app import models as models_module
    from app.graph.nodes.selection import selection_node
    from app.tools import notion

    page_id = "<page-id-001>"

    async def fake_query_pending() -> dict[str, Any]:
        return {"results": [_notion_task(page_id, 2)]}

    async def fake_update_status(page_id: str, new_status: str) -> dict[str, Any]:
        return {"id": page_id, "status": new_status}

    monkeypatch.setattr(notion, "query_pending", fake_query_pending)
    monkeypatch.setattr(notion, "update_status", fake_update_status)
    monkeypatch.setattr(
        models_module,
        "llm",
        lambda tier, **kwargs: _FakeModel(
            '{"user_message": "Try this placeholder task.", '
            f'"selected_task_id": "{page_id}"}}'
        ),
    )

    result = await selection_node(
        {
            "peer": "<recipient>",
            "incoming": "Pick something",
            "intent": "GET_TASK",
            "messages": [],
            "active_task": None,
            "streak": 0,
            "tasks_completed_today": 0,
            "user_prefs": {},
            "mood": None,
            "available_minutes": 30,
            "conversation_state": "selection",
            "pending_outbound": [],
        }
    )

    active_task = result["active_task"]
    assert active_task["rejection_count"] == 2


@pytest.mark.asyncio
async def test_rejection_node_increments_preserved_rejection_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejection node patches next Rejection Count instead of resetting to 1."""
    from app import models as models_module
    from app.graph.nodes.rejection import rejection_node
    from app.tools import notion

    patched: dict[str, Any] = {}

    async def fake_query_pending() -> dict[str, Any]:
        return {"results": []}

    async def fake_update_property(page_id: str, prop_json: dict[str, Any]) -> dict[str, Any]:
        patched["page_id"] = page_id
        patched["prop_json"] = prop_json
        return {"id": page_id}

    monkeypatch.setattr(notion, "query_pending", fake_query_pending)
    monkeypatch.setattr(notion, "update_property", fake_update_property)
    monkeypatch.setattr(
        models_module,
        "llm",
        lambda tier, **kwargs: _FakeModel(
            '{"user_message": "No problem, want something different?", '
            '"alternative_task_id": null}'
        ),
    )

    await rejection_node(
        {
            "peer": "<recipient>",
            "incoming": "Not that one",
            "intent": "REJECT",
            "messages": [],
            "active_task": {
                "page_id": "<page-id-001>",
                "title": "Placeholder task",
                "status": "In Progress",
                "work_type": "focus",
                "urgency": 50,
                "time_estimate": 30,
                "energy_required": "Medium",
                "rejection_count": 2,
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

    assert patched["prop_json"]["properties"]["Rejection Count"]["number"] == 3
