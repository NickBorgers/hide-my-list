"""Tests for attachment plumbing in app/graph/nodes/send.py.

Covers:
- Draft with attachment_path: send_node calls signal_client.send_message
  with attachment_paths=[path].
- Draft without attachment_path: signal_client.send_message called without
  attachment_paths (text-only, backward compatible).

Private data discipline: attachment_path is private. Tests use placeholder paths
and verify that no path value appears in log output.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from app.graph.state import State

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_state(**overrides: Any) -> State:
    base: State = {
        "peer": "<test-peer>",
        "incoming": "",
        "intent": "COMPLETE",
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


# ---------------------------------------------------------------------------
# Happy path: draft with attachment_path → attachment_paths passed to client
# ---------------------------------------------------------------------------

class TestSendNodeWithAttachment:
    """send_node must pass attachment_path as a single-item list to signal_client."""

    @pytest.mark.asyncio
    async def test_draft_with_attachment_path_calls_send_with_attachment(self) -> None:
        """Draft carrying attachment_path must cause send_message to receive attachment_paths."""
        from app.graph.nodes import send as send_module

        captured_kwargs: dict[str, Any] = {}

        async def fake_send_message(
            recipient: str,
            message: str,
            **kwargs: Any,
        ) -> dict[str, Any]:
            captured_kwargs["recipient"] = recipient
            captured_kwargs["message"] = message
            captured_kwargs.update(kwargs)
            return {"timestamp": 111111}

        draft: Any = {
            "recipient": "<test-recipient>",
            "body": "Nice work! ✨",
            "notion_page_id": "<page-id>",
            "attachment_path": "/placeholder/reward_artifacts/test.png",
        }

        state = _base_state(pending_outbound=[draft])

        with patch("app.tools.signal_client.send_message", new=fake_send_message):
            await send_module.send_node(state)

        assert "attachment_paths" in captured_kwargs
        assert captured_kwargs["attachment_paths"] == ["/placeholder/reward_artifacts/test.png"]

    @pytest.mark.asyncio
    async def test_draft_without_attachment_path_sends_text_only(self) -> None:
        """Draft without attachment_path must not pass attachment_paths to send_message."""
        from app.graph.nodes import send as send_module

        captured_kwargs: dict[str, Any] = {}

        async def fake_send_message(
            recipient: str,
            message: str,
            **kwargs: Any,
        ) -> dict[str, Any]:
            captured_kwargs["recipient"] = recipient
            captured_kwargs["message"] = message
            captured_kwargs.update(kwargs)
            return {"timestamp": 222222}

        draft: Any = {
            "recipient": "<test-recipient>",
            "body": "Nice work! ✨",
            "notion_page_id": "<page-id>",
        }

        state = _base_state(pending_outbound=[draft])

        with patch("app.tools.signal_client.send_message", new=fake_send_message):
            await send_module.send_node(state)

        assert "attachment_paths" not in captured_kwargs

    @pytest.mark.asyncio
    async def test_empty_pending_outbound_returns_empty(self) -> None:
        """Empty pending_outbound must return without calling send_message."""
        from app.graph.nodes import send as send_module

        call_count = 0

        async def fake_send_message(*args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {}

        state = _base_state(pending_outbound=[])

        with patch("app.tools.signal_client.send_message", new=fake_send_message):
            result = await send_module.send_node(state)

        assert result == {}
        assert call_count == 0
