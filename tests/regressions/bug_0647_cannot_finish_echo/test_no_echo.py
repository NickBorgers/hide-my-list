"""Bug #647: cannot_finish must never echo the user's message back.

Models fill an output field named `user_message` with the user's own words.
The parser must select only the user-facing fields and the prompt schema must
not offer an echo field at all. See README.md.
"""
from __future__ import annotations

import json
from typing import Any

from app.graph.nodes.cannot_finish import _parse_cannot_finish_response, cannot_finish_node

_ECHO = "this is too big, I can't finish it"
_QUESTION = "No worries — what part did you get through?"


def test_parser_prefers_progress_question_over_echo() -> None:
    raw = json.dumps(
        {"phase": "ask_progress", "user_message": _ECHO, "progress_question": _QUESTION}
    )
    assert _parse_cannot_finish_response(raw) == _QUESTION


def test_parser_prefers_next_sub_task_message_over_echo() -> None:
    raw = json.dumps(
        {
            "phase": "analyze_remaining",
            "user_message": _ECHO,
            "completed_portion": "outline",
            "next_sub_task_message": "Next up: draft the first section — ~20 min.",
        }
    )
    assert _parse_cannot_finish_response(raw) == "Next up: draft the first section — ~20 min."


def test_parser_never_selects_echo_only_json() -> None:
    """Valid JSON with no user-facing field: shame-safe fallback, not the echo, not raw JSON."""
    raw = json.dumps({"phase": "ask_progress", "user_message": _ECHO})
    parsed = _parse_cannot_finish_response(raw)
    assert parsed != _ECHO
    assert "{" not in parsed
    assert parsed == "No worries — what did you get into before stopping?"


async def test_node_replies_with_question_not_echo(monkeypatch: Any) -> None:
    """End-to-end through the node: an echo-bearing model response never reaches the user."""

    class _FakeResponse:
        content = json.dumps(
            {"phase": "ask_progress", "user_message": _ECHO, "progress_question": _QUESTION}
        )

    class _FakeModel:
        async def ainvoke(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
            return _FakeResponse()

    import app.models

    monkeypatch.setattr(app.models, "llm", lambda *_a, **_k: _FakeModel())

    state: Any = {
        "peer": "<test-peer>",
        "incoming": _ECHO,
        "active_task": {"page_id": "<placeholder-page-id>", "title": "Clean out the garage"},
        "conversation_state": "active",
    }
    update = await cannot_finish_node(state)
    body = update["pending_outbound"][0]["body"]
    assert body == _QUESTION
    assert _ECHO not in body

def test_parser_fails_closed_on_plain_text_echo() -> None:
    """Non-JSON response echoing the user's message: must return shame-safe fallback, not the echo."""
    echo = "this is too big, I can't finish it"
    result = _parse_cannot_finish_response(echo)
    assert result != echo
    assert result == "No worries — what did you get into before stopping?"


def test_parser_fails_closed_on_empty_text() -> None:
    """Empty response: must return shame-safe fallback."""
    result = _parse_cannot_finish_response("")
    assert result == "No worries — what did you get into before stopping?"
