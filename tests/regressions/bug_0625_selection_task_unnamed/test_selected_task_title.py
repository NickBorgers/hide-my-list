"""Regression for task suggestions that select a task but never name it."""
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
            "Work Type": {"select": {"name": "focus"}},
            "Time Estimate (min)": {"number": minutes},
            "Urgency": {"number": 50},
            "Energy Required": {"select": {"name": "Medium"}},
            "Rejection Count": {"number": 0},
        },
    }


def _state() -> Any:
    return {
        "peer": "<recipient>",
        "incoming": "what should I do?",
        "intent": "GET_TASK",
        "messages": [],
        "active_task": None,
        "streak": 0,
        "tasks_completed_today": 0,
        "user_prefs": {},
        "mood": None,
        "available_minutes": 30,
        "conversation_state": "idle",
        "pending_outbound": [],
    }


@pytest.fixture()
def notion_stub(monkeypatch: pytest.MonkeyPatch) -> str:
    """Stub Notion with a single pending task; returns its page id."""
    from app.tools import notion

    page_id = "<page-id-selected>"

    async def fake_query_pending() -> dict[str, Any]:
        return {"results": [_notion_task(page_id, "Placeholder selected task", 25)]}

    async def fake_update_status(pid: str, status: str) -> dict[str, Any]:
        return {"id": pid, "status": status}

    monkeypatch.setattr(notion, "query_pending", fake_query_pending)
    monkeypatch.setattr(notion, "update_status", fake_update_status)
    return page_id


@pytest.mark.asyncio
async def test_selection_draft_carries_title_when_model_omits_it(
    monkeypatch: pytest.MonkeyPatch, notion_stub: str
) -> None:
    """The exact reported shape: valid selected_task_id, body naming no task.

    Reported body was "Perfect timing - how about this focus task? It matches
    your 30 minutes and neutral mood." — a valid page id attached, but the page
    id is never rendered to the user, so the message was unactionable.
    """
    from app import models as models_module
    from app.graph.nodes.selection import selection_node

    model_response = json.dumps(
        {
            "selected_task_id": notion_stub,
            "score": 0.9,
            "reasoning": "fits time and mood",
            "user_message": (
                "Perfect timing - how about this focus task? "
                "It matches your 30 minutes and neutral mood."
            ),
        }
    )
    monkeypatch.setattr(models_module, "llm", lambda tier, **kwargs: _FakeModel(model_response))

    result = await selection_node(_state())

    draft = result["pending_outbound"][0]
    assert draft["notion_page_id"] == notion_stub
    assert draft["notion_page_title"] == "Placeholder selected task"


@pytest.mark.asyncio
async def test_sent_message_names_the_selected_task(
    monkeypatch: pytest.MonkeyPatch, notion_stub: str
) -> None:
    """End to end through send_node: the delivered text names the task."""
    from app import models as models_module
    from app.graph.nodes.selection import selection_node
    from app.graph.nodes.send import send_node
    from app.tools import signal_client

    sent: list[str] = []

    async def fake_send_message(*, recipient: str, message: str, **kwargs: Any) -> dict[str, Any]:
        sent.append(message)
        return {"timestamp": 1}

    model_response = json.dumps(
        {
            "selected_task_id": notion_stub,
            "score": 0.9,
            "reasoning": "fits time and mood",
            "user_message": "Perfect timing - how about this focus task?",
        }
    )
    monkeypatch.setattr(models_module, "llm", lambda tier, **kwargs: _FakeModel(model_response))
    monkeypatch.setattr(signal_client, "send_message", fake_send_message)

    selected = await selection_node(_state())
    state = _state()
    state["pending_outbound"] = selected["pending_outbound"]
    await send_node(state)

    assert "Placeholder selected task" in sent[0]


@pytest.mark.asyncio
async def test_task_token_is_substituted_with_exact_title(
    monkeypatch: pytest.MonkeyPatch, notion_stub: str
) -> None:
    """The prompt convention lets code own the exact selected title."""
    from app import models as models_module
    from app.graph.nodes.selection import selection_node
    from app.graph.nodes.send import send_node
    from app.tools import signal_client

    sent: list[str] = []

    async def fake_send_message(*, recipient: str, message: str, **kwargs: Any) -> dict[str, Any]:
        sent.append(message)
        return {"timestamp": 1}

    model_response = json.dumps(
        {
            "selected_task_id": notion_stub,
            "score": 0.9,
            "reasoning": "fits time and mood",
            "user_message": "Perfect timing - how about {task}? It matches your time.",
        }
    )
    monkeypatch.setattr(models_module, "llm", lambda tier, **kwargs: _FakeModel(model_response))
    monkeypatch.setattr(signal_client, "send_message", fake_send_message)

    selected = await selection_node(_state())
    state = _state()
    state["pending_outbound"] = selected["pending_outbound"]
    await send_node(state)

    assert sent[0] == "Perfect timing - how about Placeholder selected task? It matches your time."
