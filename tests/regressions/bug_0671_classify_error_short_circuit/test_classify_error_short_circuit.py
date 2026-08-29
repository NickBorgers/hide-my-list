"""Regression: classifier backend failures skip chat_node's second LLM call."""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tests.support.shame import find_banned_phrases

_FALLBACK = "Having trouble thinking right now — try again?"


def _state(incoming: str = "hello") -> dict[str, Any]:
    return {
        "peer": "<test-recipient>",
        "incoming": incoming,
        "intent": None,
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


async def _run_graph(llm_factory: Any) -> list[str]:
    from app.graph.graph import build_graph

    sent: list[str] = []

    async def fake_send_message(
        recipient: str, message: str, **_kwargs: Any
    ) -> dict[str, Any]:
        assert recipient == "<test-recipient>"
        sent.append(message)
        return {"timestamp": 1}

    graph = build_graph()
    with (
        patch("app.models.llm", new=llm_factory),
        patch("app.tools.signal_client.send_message", new=fake_send_message),
    ):
        await graph.ainvoke(
            _state(),
            config={"configurable": {"thread_id": "bug-0671"}},
        )

    return sent


@pytest.mark.asyncio
async def test_classifier_exception_sends_fallback_without_chat_llm() -> None:
    calls: list[str] = []

    class _FailingModel:
        async def ainvoke(self, _messages: list[Any]) -> Any:
            raise TimeoutError("model backend unavailable")

    def llm_factory(_tier: str, **kwargs: Any) -> _FailingModel:
        calls.append(str(kwargs.get("caller")))
        return _FailingModel()

    sent = await _run_graph(llm_factory)

    assert calls == ["classify"]
    assert sent == [_FALLBACK]
    assert find_banned_phrases(sent[0]) == []


@pytest.mark.asyncio
async def test_unusable_classifier_output_still_routes_to_chat() -> None:
    calls: list[str] = []

    class _Response:
        def __init__(self, content: str) -> None:
            self.content = content

    class _Model:
        def __init__(self, content: str) -> None:
            self._content = content

        async def ainvoke(self, _messages: list[Any]) -> _Response:
            return _Response(self._content)

    def llm_factory(_tier: str, **kwargs: Any) -> _Model:
        caller = str(kwargs.get("caller"))
        calls.append(caller)
        if caller == "classify":
            return _Model("not an intent label")
        if caller == "chat":
            return _Model("Chat fallback handled this.")
        raise AssertionError(f"unexpected caller: {caller}")

    sent = await _run_graph(llm_factory)

    assert calls == ["classify", "chat"]
    assert sent == ["Chat fallback handled this."]
