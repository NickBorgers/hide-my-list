from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

import pytest


class _SilentStream:
    """Async iterator that blocks indefinitely; returned by _SilentWebSocket.__aiter__."""

    def __init__(self, block: asyncio.Event) -> None:
        self._block = block

    def __aiter__(self) -> _SilentStream:
        return self

    async def __anext__(self) -> str:
        await self._block.wait()
        raise AssertionError("unreachable")


class _SilentWebSocket:
    """Async iterable only (no __anext__), matching websockets 16 ClientConnection."""

    def __init__(self) -> None:
        self._never_delivers = asyncio.Event()

    def __aiter__(self) -> _SilentStream:
        return _SilentStream(self._never_delivers)


class _ConnectContext:
    async def __aenter__(self) -> _SilentWebSocket:
        return _SilentWebSocket()

    async def __aexit__(self, *_args: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_receive_stream_idle_timeout_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools import signal_client

    original_sleep = asyncio.sleep
    second_connect = asyncio.Event()
    connect_count = 0
    sleep_calls: list[float] = []

    def fake_connect(_endpoint: str) -> _ConnectContext:
        nonlocal connect_count
        connect_count += 1
        if connect_count == 2:
            second_connect.set()
        return _ConnectContext()

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        await original_sleep(0)

    monkeypatch.setenv("SIGNAL_RECEIVE_IDLE_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setattr(signal_client.websockets, "connect", fake_connect)
    monkeypatch.setattr(signal_client.asyncio, "sleep", fake_sleep)

    messages = signal_client.receive_messages(
        base_url="http://signal-cli-test:8080",
        account="<account>",
    )
    task = asyncio.create_task(anext(messages))
    try:
        await asyncio.wait_for(second_connect.wait(), timeout=1)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await messages.aclose()

    assert connect_count >= 2
    assert sleep_calls == [1.0]
