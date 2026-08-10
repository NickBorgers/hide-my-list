"""Tests for multi-turn conversation context flow.

Covers the message-channel write contract that lets short follow-ups resolve
against the prior turn:

- send_node must append HumanMessage(incoming) + AIMessage(body) per outbound
  draft to state["messages"] so the next turn's classifier and intent nodes
  see history.
- classify_intent must include the windowed prior conversation in the prompt
  it sends to the LLM. Without that, "by Friday" after an ADD_TASK turn looks
  like CHAT.

Private data discipline: tests use placeholder peer/body values.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.graph.state import State


def _base_state(**overrides: Any) -> State:
    base: State = {
        "peer": "<test-peer>",
        "incoming": "",
        "intent": "CHAT",
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
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


class TestSendNodeAppendsMessages:
    """send_node must populate state['messages'] so the next turn has context."""

    @pytest.mark.asyncio
    async def test_appends_human_and_ai_messages_for_each_draft(self) -> None:
        from app.graph.nodes import send as send_module

        async def fake_send_message(
            recipient: str, message: str, **kwargs: Any
        ) -> dict[str, Any]:
            return {"timestamp": 1}

        draft: Any = {
            "recipient": "<test-recipient>",
            "body": "Got it — added.",
            "notion_page_id": "<page-id>",
        }
        state = _base_state(incoming="I need to call the dentist", pending_outbound=[draft])

        with patch("app.tools.signal_client.send_message", new=fake_send_message):
            result = await send_module.send_node(state)

        assert "messages" in result
        appended = result["messages"]
        assert len(appended) == 2
        assert isinstance(appended[0], HumanMessage)
        assert appended[0].content == "I need to call the dentist"
        assert isinstance(appended[1], AIMessage)
        assert appended[1].content == "Got it — added."

    @pytest.mark.asyncio
    async def test_no_messages_when_nothing_to_record(self) -> None:
        """Empty incoming and empty pending must keep the return dict empty."""
        from app.graph.nodes import send as send_module

        state = _base_state(incoming="", pending_outbound=[])
        result = await send_module.send_node(state)
        assert result == {}


class TestClassifyIntentUsesHistory:
    """classify_intent must pass windowed prior history into the LLM prompt."""

    @pytest.mark.asyncio
    async def test_prior_messages_appear_in_classifier_prompt(self) -> None:
        from app.graph import routing

        captured: dict[str, Any] = {}

        class _FakeResp:
            content = "ADD_TASK"

        class _FakeModel:
            async def ainvoke(self, msgs: list[Any]) -> Any:
                captured["msgs"] = msgs
                return _FakeResp()

        def _fake_llm(_tier: str, **_kwargs: Any) -> Any:
            return _FakeModel()

        prior = [
            HumanMessage(content="I need to call Test Vendor"),
            AIMessage(content="Got it — independent task, ~20 min."),
        ]
        state = _base_state(incoming="I need to do it by Friday", messages=prior)

        with patch("app.models.llm", new=_fake_llm):
            result = await routing.classify_intent(state)

        assert result == {"intent": "ADD_TASK"}
        msgs = captured["msgs"]
        # Second message is the HumanMessage carrying the prompt body.
        human_content = msgs[1].content
        assert "Test Vendor" in human_content
        assert "I need to do it by Friday" in human_content
        assert "Prior conversation:" in human_content

    @pytest.mark.asyncio
    async def test_empty_history_uses_no_prior_context_placeholder(self) -> None:
        from app.graph import routing

        captured: dict[str, Any] = {}

        class _FakeResp:
            content = "CHAT"

        class _FakeModel:
            async def ainvoke(self, msgs: list[Any]) -> Any:
                captured["msgs"] = msgs
                return _FakeResp()

        def _fake_llm(_tier: str, **_kwargs: Any) -> Any:
            return _FakeModel()

        state = _base_state(incoming="Hello", messages=[])

        with patch("app.models.llm", new=_fake_llm):
            await routing.classify_intent(state)

        human_content = captured["msgs"][1].content
        assert "No prior context." in human_content
