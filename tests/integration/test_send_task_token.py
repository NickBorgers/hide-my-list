"""Integration test: render_task_token reachable through send_node end-to-end.

Clause 1 of the test-rig contract requires every new public function under
app/graph/nodes/ to have integration coverage asserting reachability from an
end-to-end flow. render_task_token lives in app/graph/nodes/_task_token.py
and is called by send_node for every draft that carries notion_page_title.

These tests drive send_node with mocked Signal and assert the final Signal
kwargs contain the exact substituted title — covering the full
draft-in → render_task_token → signal_client path.
"""
from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import patch

import pytest

from app.graph.state import State


def _state_with_draft(
    body: str,
    notion_page_title: str | None = None,
    notion_page_id: str = "<placeholder-page-id>",
) -> State:
    draft: dict[str, Any] = {
        "recipient": "<test-recipient>",
        "body": body,
        "notion_page_id": notion_page_id,
    }
    if notion_page_title is not None:
        draft["notion_page_title"] = notion_page_title
    return {  # type: ignore[return-value]
        "peer": "<test-recipient>",
        "incoming": "",
        "messages": [],
        "pending_outbound": [draft],
    }


@pytest.mark.asyncio
async def test_send_node_substitutes_task_token_via_render_task_token() -> None:
    """send_node calls render_task_token; {task} in body is replaced with stored title.

    End-to-end path: State → send_node → render_task_token → signal_client.
    Asserts reachability of render_task_token from the node layer.
    """
    sent: list[dict[str, Any]] = []

    async def mock_send(*, recipient: str, message: str, **kwargs: Any) -> dict[str, Any]:
        sent.append({"recipient": recipient, "message": message, **kwargs})
        return {"timestamp": 1}

    with patch("app.tools.signal_client.send_message", side_effect=mock_send):
        from app.graph.nodes.send import send_node

        await send_node(
            _state_with_draft(
                body="How about {task}? Fits your 30 minutes.",
                notion_page_title="Placeholder selected task",
            )
        )

    assert len(sent) == 1
    assert sent[0]["message"] == "How about Placeholder selected task? Fits your 30 minutes."


@pytest.mark.asyncio
async def test_send_node_appends_title_when_body_omits_token() -> None:
    """send_node falls back to appending task title when body omits {task} entirely."""
    sent: list[dict[str, Any]] = []

    async def mock_send(*, recipient: str, message: str, **kwargs: Any) -> dict[str, Any]:
        sent.append({"recipient": recipient, "message": message, **kwargs})
        return {"timestamp": 1}

    with patch("app.tools.signal_client.send_message", side_effect=mock_send):
        from app.graph.nodes.send import send_node

        await send_node(
            _state_with_draft(
                body="Perfect timing — how about this focus task?",
                notion_page_title="Placeholder selected task",
            )
        )

    assert len(sent) == 1
    assert "Placeholder selected task" in sent[0]["message"]


@pytest.mark.asyncio
async def test_send_node_idempotency_key_uses_substituted_body() -> None:
    """Idempotency key is hashed from the final (post-substitution) body."""
    sent: list[dict[str, Any]] = []

    async def mock_send(*, recipient: str, message: str, **kwargs: Any) -> dict[str, Any]:
        sent.append({"recipient": recipient, "message": message, **kwargs})
        return {"timestamp": 1}

    with patch("app.tools.signal_client.send_message", side_effect=mock_send):
        from app.graph.nodes.send import send_node

        await send_node(
            _state_with_draft(
                body="How about {task}?",
                notion_page_title="Placeholder selected task",
            )
        )

    assert len(sent) == 1
    final_message = sent[0]["message"]
    assert final_message == "How about Placeholder selected task?"
    expected_key = hashlib.sha256(f"<test-recipient>:{final_message}".encode()).hexdigest()[:32]
    assert sent[0]["idempotency_key"] == expected_key


@pytest.mark.asyncio
async def test_send_node_draft_without_title_bypasses_render_task_token() -> None:
    """Drafts without notion_page_title skip substitution — body sent as-is."""
    sent: list[dict[str, Any]] = []

    async def mock_send(*, recipient: str, message: str, **kwargs: Any) -> dict[str, Any]:
        sent.append({"recipient": recipient, "message": message, **kwargs})
        return {"timestamp": 1}

    with patch("app.tools.signal_client.send_message", side_effect=mock_send):
        from app.graph.nodes.send import send_node

        body = "Nice work! Ready for another task?"
        await send_node(_state_with_draft(body=body))

    assert len(sent) == 1
    assert sent[0]["message"] == body
