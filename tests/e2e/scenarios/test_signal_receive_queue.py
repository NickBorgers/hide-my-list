"""Signal receive queue regression.

This scenario uses the real compiled graph and Postgres checkpointer, while the
LLM is deliberately faked so the first graph turn can be held open long enough
to prove the listener keeps reading the Signal stream.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.support.signal_sink import SignalSink

pytestmark = pytest.mark.asyncio


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _SlowFirstModel:
    def __init__(
        self,
        *,
        caller: str,
        first_call_started: asyncio.Event,
        release_first_call: asyncio.Event,
        first_call_seen: asyncio.Event,
    ) -> None:
        self._caller = caller
        self._first_call_started = first_call_started
        self._release_first_call = release_first_call
        self._first_call_seen = first_call_seen

    async def ainvoke(self, _messages: list[Any]) -> _FakeResponse:
        if not self._first_call_seen.is_set():
            self._first_call_seen.set()
            self._first_call_started.set()
            await self._release_first_call.wait()
        if self._caller == "classify":
            return _FakeResponse("CHAT")
        return _FakeResponse("ok")


class _ObservedGraph:
    def __init__(self, graph: Any) -> None:
        self._graph = graph
        self.inputs: list[dict[str, Any]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def ainvoke(self, state: dict[str, Any], config: dict[str, Any]) -> Any:
        self.inputs.append(dict(state))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            return await self._graph.ainvoke(state, config)
        finally:
            self.in_flight -= 1


async def _wait_for(predicate: Any, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


def _envelope(peer: str, text: str, timestamp: int) -> dict[str, Any]:
    return {
        "envelope": {
            "source": peer,
            "timestamp": timestamp,
            "dataMessage": {"message": text, "timestamp": timestamp},
        }
    }


async def test_stacked_messages_are_read_and_coalesced_behind_slow_graph(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.graph.graph import build_graph, build_postgres_checkpointer
    from app.ingress import signal_listener as listener_module
    from app.ingress.signal_listener import SignalListener

    peer = "+15550001234"
    inbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    signal = SignalSink()
    first_call_started = asyncio.Event()
    release_first_call = asyncio.Event()
    first_call_seen = asyncio.Event()

    def fake_llm(tier: Any, *, caller: str = "unknown") -> _SlowFirstModel:
        _ = tier
        return _SlowFirstModel(
            caller=caller,
            first_call_started=first_call_started,
            release_first_call=release_first_call,
            first_call_seen=first_call_seen,
        )

    async def fake_receive_messages(**_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        while True:
            yield await inbound.get()

    monkeypatch.setattr("app.models.llm", fake_llm)
    undo_signal = signal.install()
    original_receive = listener_module.receive_messages
    listener_module.receive_messages = fake_receive_messages

    try:
        async with build_postgres_checkpointer(database_url) as checkpointer:
            observed = _ObservedGraph(build_graph(checkpointer=checkpointer))
            listener = SignalListener(
                account="+15550009999",
                graph=observed,
                authorized_peers=frozenset({peer}),
                message_debounce_seconds=0.05,
            )
            runner = asyncio.create_task(listener.run())
            try:
                inbound.put_nowait(_envelope(peer, "first", 100))
                await asyncio.wait_for(first_call_started.wait(), timeout=2)

                inbound.put_nowait(_envelope(peer, "second", 200))
                inbound.put_nowait(_envelope(peer, "third", 300))
                await _wait_for(lambda: len(signal.read_receipts) == 3)
                assert release_first_call.is_set() is False

                release_first_call.set()
                await _wait_for(lambda: len(observed.inputs) == 2 and len(signal.sent) == 2)
            finally:
                runner.cancel()
                try:
                    await runner
                except asyncio.CancelledError:
                    pass
    finally:
        listener_module.receive_messages = original_receive
        undo_signal()

    assert [timestamp for _, timestamp in signal.read_receipts] == [100, 200, 300]
    assert observed.inputs[0]["incoming"] == "first"
    assert observed.inputs[1]["incoming"] == "second\nthird"
    assert observed.max_in_flight == 1
