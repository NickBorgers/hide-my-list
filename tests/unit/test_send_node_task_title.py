"""send_node enforces the task-naming invariant for every draft.

A draft that carries `notion_page_title` is asserting that its body names that
task. send_node guarantees it, so no node — including ones added later — can
send a suggestion the user cannot act on.
"""
from __future__ import annotations

import hashlib
import inspect
from typing import Any
from unittest.mock import AsyncMock

import pytest


class _CapturingSignalClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, *, recipient: str, message: str, **kwargs: Any) -> dict[str, Any]:
        self.sent.append({"recipient": recipient, "message": message, **kwargs})
        return {"timestamp": 1}


def _state(draft: dict[str, Any]) -> Any:
    return {
        "peer": "<recipient>",
        "incoming": "",
        "messages": [],
        "pending_outbound": [draft],
    }


@pytest.fixture()
def signal(monkeypatch: pytest.MonkeyPatch) -> _CapturingSignalClient:
    from app.tools import signal_client

    capturing = _CapturingSignalClient()
    monkeypatch.setattr(signal_client, "send_message", capturing.send_message)
    return capturing


@pytest.mark.asyncio
async def test_body_missing_title_gets_named(signal: _CapturingSignalClient) -> None:
    """The reported failure: valid page id, body naming no task."""
    from app.graph.nodes.send import send_node

    await send_node(
        _state(
            {
                "recipient": "<recipient>",
                "body": "Perfect timing - how about this focus task?",
                "notion_page_id": "<page-id>",
                "notion_page_title": "Placeholder selected task",
            }
        )
    )

    assert "Placeholder selected task" in signal.sent[0]["message"]


@pytest.mark.asyncio
async def test_task_token_is_replaced_exactly(signal: _CapturingSignalClient) -> None:
    from app.graph.nodes.send import send_node

    await send_node(
        _state(
            {
                "recipient": "<recipient>",
                "body": "Perfect timing - how about {task}?",
                "notion_page_id": "<page-id>",
                "notion_page_title": "Placeholder selected task",
            }
        )
    )

    assert signal.sent[0]["message"] == "Perfect timing - how about Placeholder selected task?"


@pytest.mark.asyncio
async def test_body_already_naming_task_is_untouched(signal: _CapturingSignalClient) -> None:
    """No double-naming when the model already got it right."""
    from app.graph.nodes.send import send_node

    body = "How about Placeholder selected task? Fits your window."
    await send_node(
        _state(
            {
                "recipient": "<recipient>",
                "body": body,
                "notion_page_id": "<page-id>",
                "notion_page_title": "Placeholder selected task",
            }
        )
    )

    assert signal.sent[0]["message"] == body


@pytest.mark.asyncio
async def test_draft_without_title_is_untouched(signal: _CapturingSignalClient) -> None:
    """Drafts that opt out — a completion celebration, say — are left alone."""
    from app.graph.nodes.send import send_node

    body = "Done! Nice work. Want another task?"
    await send_node(
        _state(
            {
                "recipient": "<recipient>",
                "body": body,
                "notion_page_id": "<page-id>",
            }
        )
    )

    assert signal.sent[0]["message"] == body


@pytest.mark.asyncio
async def test_idempotency_key_matches_sent_body(signal: _CapturingSignalClient) -> None:
    """The key is hashed from the substituted body, not the pre-injection draft."""
    from app.graph.nodes.send import send_node

    await send_node(
        _state(
            {
                "recipient": "<recipient>",
                "body": "How about {task}?",
                "notion_page_id": "<page-id>",
                "notion_page_title": "Placeholder selected task",
            }
        )
    )

    sent = signal.sent[0]
    expected = hashlib.sha256(f"<recipient>:{sent['message']}".encode()).hexdigest()[:32]
    assert sent["idempotency_key"] == expected


@pytest.mark.asyncio
async def test_send_message_kwargs_match_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """send_node kwarg names must match signal_client.send_message's signature."""
    from app.graph.nodes.send import send_node
    from app.tools import signal_client

    sig_params = set(inspect.signature(signal_client.send_message).parameters.keys())

    mock = AsyncMock(return_value={"timestamp": 1})
    monkeypatch.setattr(signal_client, "send_message", mock)

    await send_node(
        _state(
            {
                "recipient": "<recipient>",
                "body": "How about {task}?",
                "notion_page_id": "<page-id>",
                "notion_page_title": "Placeholder selected task",
            }
        )
    )

    assert mock.called
    called_kwargs = set(mock.call_args.kwargs.keys())
    unknown = called_kwargs - sig_params
    assert not unknown, f"send_node passed unknown kwargs to send_message: {unknown}"
    assert "recipient" in called_kwargs
    assert "message" in called_kwargs
    assert "idempotency_key" in called_kwargs
    assert "attachment_paths" not in called_kwargs
