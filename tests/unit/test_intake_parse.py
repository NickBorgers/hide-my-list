"""Unit tests for intake's response parser.

`_parse_intake_response` must distinguish a parseable LLM response from an
unparseable one. The bug this guards against: when the parser could not extract
JSON it silently returned a fabricated default dict (is_reminder=False,
confirmation="Got it — added."), so a truncated/garbled model response was
indistinguishable from a real "plain task" and any reminder in the message was
silently dropped. The parser must now return None on failure so the node can
handle it honestly (raw-save + ops alert + truthful confirmation).
"""
from __future__ import annotations

import json

from app.graph.nodes.intake import _parse_intake_response


def test_valid_json_returns_dict() -> None:
    payload = {
        "action": "save",
        "title": "Placeholder task",
        "is_reminder": True,
        "remind_at": "2026-01-02T17:00:00-06:00",
        "confirmation_message": "Got it — I'll remind you at 5pm.",
    }
    parsed = _parse_intake_response(json.dumps(payload))
    assert parsed is not None
    assert parsed["action"] == "save"
    assert parsed["is_reminder"] is True
    assert parsed["remind_at"] == "2026-01-02T17:00:00-06:00"


def test_json_embedded_in_prose_is_extracted() -> None:
    text = 'Here is the result:\n{"action": "save", "title": "x"}\nDone.'
    parsed = _parse_intake_response(text)
    assert parsed is not None
    assert parsed["title"] == "x"


def test_truncated_json_returns_none() -> None:
    """A response cut off at the output-token ceiling has no closing brace."""
    truncated = '{"action": "save", "title": "clean the fountain", "is_reminder": tr'
    assert _parse_intake_response(truncated) is None


def test_non_json_prose_returns_none() -> None:
    assert _parse_intake_response("Sure, I can help you set that up!") is None


def test_empty_response_returns_none() -> None:
    assert _parse_intake_response("") is None
