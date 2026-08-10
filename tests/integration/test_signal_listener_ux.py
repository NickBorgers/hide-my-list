"""Integration coverage for Signal listener UX side effects."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None


class _FakeAsyncClient:
    calls: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.base_url = kwargs.get("base_url")

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, *, json: dict[str, Any]) -> _FakeResponse:
        self.calls.append(
            {
                "kind": "receipt",
                "base_url": self.base_url,
                "url": url,
                "json": json,
            }
        )
        return _FakeResponse()

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any],
    ) -> _FakeResponse:
        self.calls.append(
            {
                "kind": "typing",
                "method": method,
                "base_url": self.base_url,
                "url": url,
                "json": json,
            }
        )
        return _FakeResponse()


async def _async_gen(envelopes: list[dict[str, Any]]):
    for envelope in envelopes:
        yield envelope


async def _wait_for_call_count(expected: int) -> None:
    for _ in range(20):
        if len(_FakeAsyncClient.calls) >= expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected {expected} Signal UX calls")


@pytest.mark.asyncio
async def test_authorized_inbound_text_reaches_receipt_and_typing_tools() -> None:
    """Authorized text drives listener through public Signal UX tool functions."""
    from app.ingress.signal_listener import SignalListener

    _FakeAsyncClient.calls = []
    events: list[str] = []

    async def fake_graph_ainvoke(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(0)
        events.append("graph")
        await asyncio.sleep(0)

    graph = AsyncMock()
    graph.ainvoke.side_effect = fake_graph_ainvoke

    listener = SignalListener(
        graph=graph,
        base_url="http://signal-cli-test:8080",
        account="<test-account>",
        authorized_peers=frozenset({"<test-recipient>"}),
    )
    envelopes = [
        {
            "envelope": {
                "source": "<test-recipient>",
                "timestamp": 1_716_800_000_000,
                "dataMessage": {"message": "Test message"},
            }
        }
    ]

    with (
        patch("app.ingress.signal_listener.receive_messages", return_value=_async_gen(envelopes)),
        patch("httpx.AsyncClient", _FakeAsyncClient),
    ):
        await listener.run()
        await _wait_for_call_count(3)

    assert graph.ainvoke.await_count == 1
    assert events == ["graph"]

    receipt_call = _FakeAsyncClient.calls[0]
    typing_start_call = _FakeAsyncClient.calls[1]
    typing_stop_call = _FakeAsyncClient.calls[2]

    assert receipt_call == {
        "kind": "receipt",
        "base_url": "http://signal-cli-test:8080",
        "url": "/v1/receipts/<test-account>",
        "json": {
            "recipient": "<test-recipient>",
            "receipt_type": "read",
            "timestamp": 1_716_800_000_000,
        },
    }
    assert typing_start_call == {
        "kind": "typing",
        "method": "PUT",
        "base_url": "http://signal-cli-test:8080",
        "url": "/v1/typing-indicator/<test-account>",
        "json": {"recipient": "<test-recipient>"},
    }
    assert typing_stop_call == {
        "kind": "typing",
        "method": "DELETE",
        "base_url": "http://signal-cli-test:8080",
        "url": "/v1/typing-indicator/<test-account>",
        "json": {"recipient": "<test-recipient>"},
    }
