"""Fixtures for the end-to-end conversation layer.

The repo's first `conftest.py`, and deliberately directory-scoped: every other
layer keeps its inline-fixture style, and nothing outside `tests/e2e/` is
affected by anything defined here.

This layer calls the **real** LLM through the LiteLLM proxy. Notion and Signal
are faked; the model is not. That means it cannot run on a GitHub-hosted runner
(the proxy is tailnet-only) and it costs wall-clock, so it is gated on
`ENABLE_E2E_CONVERSATIONS` and metered against `E2E_MAX_LLM_CALLS`.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from tests.support.harness import Conversation, _ObservedGraph
from tests.support.notion_fake import FakeNotion
from tests.support.signal_sink import SignalSink

_ENABLE_KEY = "ENABLE_E2E_CONVERSATIONS"
_DEFAULT_MAX_CALLS = 120


def _enabled() -> bool:
    return os.environ.get(_ENABLE_KEY, "").lower() in ("true", "1", "yes")


def _missing_env() -> list[str]:
    return [
        name
        for name in ("DATABASE_URL", "LLM_PROXY_BASE_URL", "LLM_PROXY_API_KEY")
        if not os.environ.get(name)
    ]


pytestmark = pytest.mark.skipif(
    not _enabled(), reason=f"{_ENABLE_KEY} not set; the live conversation layer is opt-in"
)


def pytest_collection_modifyitems(items: list[Any]) -> None:
    """Skip the whole directory unless the layer is enabled and configured."""
    if _enabled() and not _missing_env():
        return
    reason = (
        f"{_ENABLE_KEY} not set; the live conversation layer is opt-in"
        if not _enabled()
        else f"missing required env: {', '.join(_missing_env())}"
    )
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        item.add_marker(skip)


class _CallMeter:
    """Counts live LLM calls against E2E_MAX_LLM_CALLS.

    No new instrumentation: `LLMObservabilityCallback` is attached to every model
    `app.models.llm()` returns and emits `llm.call.end` with token counts, and
    each turn already runs inside `structlog.testing.capture_logs()` for
    invariant I1. Metering is just reading that stream.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.calls = 0
        self.total_tokens = 0

    def record(self, logs: list[dict[str, Any]]) -> None:
        for entry in logs:
            if entry.get("event") != "llm.call.end":
                continue
            self.calls += 1
            self.total_tokens += int(entry.get("total_tokens") or 0)
        if self.calls > self.limit:
            pytest.fail(
                f"E2E_MAX_LLM_CALLS exceeded: {self.calls} > {self.limit}. "
                "Either a scenario is looping or the suite has outgrown its budget."
            )


@pytest.fixture(scope="session")
def call_meter() -> Iterator[_CallMeter]:
    limit = int(os.environ.get("E2E_MAX_LLM_CALLS", str(_DEFAULT_MAX_CALLS)))
    meter = _CallMeter(limit)
    yield meter
    print(  # noqa: T201 — surfaced in the CI job summary
        f"\n[e2e] {meter.calls} live LLM calls, {meter.total_tokens} tokens"
    )


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ["DATABASE_URL"]
    from app.tools.db import run_migrations

    run_migrations()
    return url


@pytest.fixture()
def peer() -> str:
    """A fresh E.164 per test.

    `thread_id` is the peer verbatim, so a unique peer means a unique
    checkpoint — tests cannot inherit each other's conversation history.
    """
    return f"+1555{uuid.uuid4().int % 10_000_000:07d}"


@asynccontextmanager
async def _live_conversations(
    peers: list[str], database_url: str, call_meter: _CallMeter
) -> AsyncIterator[list[Conversation]]:
    """Stand up one listener, one graph, and one faked world for `peers`.

    Everything except the peer identity is shared, which mirrors production:
    one signal-cli account, one single-tenant Notion database, one checkpointer.
    Only `thread_id` — the peer E.164 — separates the conversations, which is
    exactly the property scenario 7 is testing.
    """
    from app.graph.graph import build_graph, build_postgres_checkpointer
    from app.ingress import signal_listener as listener_module
    from app.ingress.signal_listener import SignalListener

    notion = FakeNotion()
    signal = SignalSink()
    undo_notion = notion.install()
    undo_signal = signal.install()

    inbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def fake_receive_messages(**_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        while True:
            yield await inbound.get()

    original_receive = listener_module.receive_messages
    listener_module.receive_messages = fake_receive_messages

    try:
        async with build_postgres_checkpointer(database_url) as checkpointer:
            graph = build_graph(checkpointer=checkpointer)
            observed = _ObservedGraph(graph)
            listener = SignalListener(
                account="+15550009999",
                graph=observed,
                authorized_peers=frozenset(peers),
            )
            runner = asyncio.create_task(listener.run())
            try:
                yield [
                    Conversation(
                        peer=peer,
                        graph=graph,
                        observed=observed,
                        notion=notion,
                        signal=signal,
                        database_url=database_url,
                        enqueue_envelope=inbound.put_nowait,
                        call_meter=call_meter,
                    )
                    for peer in peers
                ]
            finally:
                runner.cancel()
                try:
                    await runner
                except asyncio.CancelledError:
                    pass
    finally:
        listener_module.receive_messages = original_receive
        undo_signal()
        undo_notion()


@pytest.fixture()
async def conversation(
    peer: str, database_url: str, call_meter: _CallMeter
) -> AsyncIterator[Conversation]:
    """A live conversation: real graph, real Postgres, real LLM, faked I/O."""
    async with _live_conversations([peer], database_url, call_meter) as conversations:
        yield conversations[0]


@pytest.fixture()
async def conversation_pair(
    peer: str, database_url: str, call_meter: _CallMeter
) -> AsyncIterator[tuple[Conversation, Conversation]]:
    """Two authorized peers behind one listener, one graph, one checkpointer."""
    second = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
    async with _live_conversations([peer, second], database_url, call_meter) as conversations:
        yield conversations[0], conversations[1]
