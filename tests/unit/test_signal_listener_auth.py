"""Tests for the AUTHORIZED_PEERS allowlist in app.ingress.signal_listener.

Notion is single-tenant; the listener must drop messages from any peer
not in AUTHORIZED_PEERS before the graph is invoked. The listener must
refuse to start when AUTHORIZED_PEERS is unset or empty (fail-safe
closed default).
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


def _envelope(
    source: str,
    message: str,
    timestamp: int | None = 1_716_800_000_000,
) -> dict[str, Any]:
    """Build a minimal signal-cli envelope for tests."""
    envelope: dict[str, Any] = {
        "envelope": {
            "source": source,
            "dataMessage": {"message": message},
        }
    }
    if timestamp is not None:
        envelope["envelope"]["timestamp"] = timestamp
    return envelope


def _reaction_envelope(
    source: str,
    emoji: str = "👍",
    target_sent_timestamp: int = 1_716_800_000_000,
    target_author: str = "+15559876543",
    *,
    is_remove: bool = False,
) -> dict[str, Any]:
    """Build a minimal signal-cli reaction envelope for tests."""
    return {
        "envelope": {
            "source": source,
            "dataMessage": {
                "reaction": {
                    "emoji": emoji,
                    "targetAuthor": target_author,
                    "targetSentTimestamp": target_sent_timestamp,
                    "isRemove": is_remove,
                }
            },
        }
    }


async def _async_gen(envelopes: list[dict[str, Any]]):
    for env in envelopes:
        yield env


def test_load_authorized_peers_empty_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty/unset AUTHORIZED_PEERS must raise — open ingress is not a default."""
    monkeypatch.delenv("AUTHORIZED_PEERS", raising=False)
    from app.ingress.signal_listener import _load_authorized_peers

    with pytest.raises(RuntimeError, match="AUTHORIZED_PEERS is empty"):
        _load_authorized_peers()


def test_load_authorized_peers_whitespace_only_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank-string AUTHORIZED_PEERS must also refuse."""
    monkeypatch.setenv("AUTHORIZED_PEERS", "  , , ")
    from app.ingress.signal_listener import _load_authorized_peers

    with pytest.raises(RuntimeError, match="AUTHORIZED_PEERS is empty"):
        _load_authorized_peers()


def test_load_authorized_peers_parses_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Comma-separated list parses into a frozenset with whitespace trimmed."""
    monkeypatch.setenv("AUTHORIZED_PEERS", " +15551234567 , +15559876543 ")
    from app.ingress.signal_listener import _load_authorized_peers

    peers = _load_authorized_peers()
    assert peers == frozenset({"+15551234567", "+15559876543"})


def test_extract_reaction_parses_signal_payload() -> None:
    """Signal reaction envelopes parse into peer, emoji, and target timestamp."""
    from app.ingress.signal_listener import _extract_reaction

    result = _extract_reaction(_reaction_envelope("+15551234567"))

    assert result == ("+15551234567", "👍", 1_716_800_000_000, "+15559876543")


def test_extract_reaction_skips_removed_reaction() -> None:
    """Un-react events must not be recorded as feedback."""
    from app.ingress.signal_listener import _extract_reaction

    assert _extract_reaction(_reaction_envelope("+15551234567", is_remove=True)) is None


def test_extract_reaction_returns_none_for_text_message() -> None:
    """Text messages fall through to the normal graph path."""
    from app.ingress.signal_listener import _extract_reaction

    assert _extract_reaction(_envelope("+15551234567", "hello")) is None


def test_listener_construct_fails_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Listener construction reads env on init — fail-fast at startup."""
    monkeypatch.delenv("AUTHORIZED_PEERS", raising=False)
    from app.ingress.signal_listener import SignalListener

    with pytest.raises(RuntimeError, match="AUTHORIZED_PEERS is empty"):
        SignalListener(graph=object())


@pytest.mark.asyncio
async def test_authorized_peer_invokes_graph() -> None:
    """A peer in AUTHORIZED_PEERS reaches the graph."""
    from app.ingress.signal_listener import SignalListener

    graph = AsyncMock()
    listener = SignalListener(
        graph=graph,
        authorized_peers=frozenset({"+15551234567"}),
    )

    envelopes = [_envelope("+15551234567", "hello")]
    with patch(
        "app.ingress.signal_listener.receive_messages",
        return_value=_async_gen(envelopes),
    ):
        await listener.run()

    graph.ainvoke.assert_awaited_once()
    args, kwargs = graph.ainvoke.await_args
    assert args[0]["peer"] == "+15551234567"
    assert args[0]["incoming"] == "hello"
    assert kwargs["config"]["configurable"]["thread_id"] == "+15551234567"


@pytest.mark.asyncio
async def test_authorized_peer_schedules_receipt_and_typing_around_graph() -> None:
    """Receipt and typing UX signals are scheduled around graph invocation."""
    from app.ingress.signal_listener import SignalListener

    events: list[str] = []

    async def fake_graph_ainvoke(*args: Any, **kwargs: Any) -> None:
        events.append("graph")

    def fake_start_background_task(coro: Any) -> None:
        name = getattr(getattr(coro, "cr_code", None), "co_name", "")
        events.append(name)
        coro.close()

    graph = AsyncMock()
    graph.ainvoke.side_effect = fake_graph_ainvoke
    listener = SignalListener(
        graph=graph,
        base_url="http://signal-cli-test:8080",
        account="<test-account>",
        authorized_peers=frozenset({"+15551234567"}),
    )

    envelopes = [_envelope("+15551234567", "hello", timestamp=1_716_800_000_000)]
    with (
        patch("app.ingress.signal_listener.receive_messages", return_value=_async_gen(envelopes)),
        patch(
            "app.ingress.signal_listener._start_background_task",
            new=fake_start_background_task,
        ),
    ):
        await listener.run()

    assert events == [
        "send_read_receipt",
        "_maintain_typing_indicator",
        "graph",
        "send_typing_indicator",
    ]


@pytest.mark.asyncio
async def test_typing_indicator_refreshes_until_stopped() -> None:
    """Long graph work gets repeated typing-start refreshes."""
    from app.ingress import signal_listener

    calls: list[dict[str, Any]] = []
    stop_event = asyncio.Event()

    async def fake_send_typing_indicator(
        peer: str,
        *,
        started: bool = True,
        base_url: str | None = None,
        account: str | None = None,
    ) -> None:
        calls.append(
            {
                "peer": peer,
                "started": started,
                "base_url": base_url,
                "account": account,
            }
        )
        if len(calls) == 2:
            stop_event.set()

    with patch(
        "app.ingress.signal_listener.send_typing_indicator",
        new=fake_send_typing_indicator,
    ):
        await signal_listener._maintain_typing_indicator(
            peer="+15551234567",
            stop_event=stop_event,
            base_url="http://signal-cli-test:8080",
            account="<test-account>",
            refresh_seconds=0,
        )

    assert calls == [
        {
            "peer": "+15551234567",
            "started": True,
            "base_url": "http://signal-cli-test:8080",
            "account": "<test-account>",
        },
        {
            "peer": "+15551234567",
            "started": True,
            "base_url": "http://signal-cli-test:8080",
            "account": "<test-account>",
        },
    ]


@pytest.mark.asyncio
async def test_unauthorized_peer_silently_dropped() -> None:
    """A peer NOT in AUTHORIZED_PEERS is dropped before the graph is invoked."""
    from app.ingress.signal_listener import SignalListener

    graph = AsyncMock()
    listener = SignalListener(
        graph=graph,
        authorized_peers=frozenset({"+15551234567"}),
    )

    envelopes = [_envelope("+19990001111", "hello from attacker")]
    with patch(
        "app.ingress.signal_listener.receive_messages",
        return_value=_async_gen(envelopes),
    ):
        await listener.run()

    # Graph never reached — the silent drop must happen before invocation.
    graph.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_reaction_records_feedback_without_graph() -> None:
    """Authorized reactions are routed to the feedback handler, not the graph."""
    from app.ingress.signal_listener import SignalListener

    graph = AsyncMock()
    listener = SignalListener(
        graph=graph,
        account="+15559876543",  # must match default target_author in _reaction_envelope
        authorized_peers=frozenset({"+15551234567"}),
    )

    record_feedback = AsyncMock(return_value=True)
    envelopes = [_reaction_envelope("+15551234567", emoji="👍")]
    with (
        patch("app.ingress.signal_listener.receive_messages", return_value=_async_gen(envelopes)),
        patch("app.tools.rewards.record_reward_feedback", new=record_feedback),
    ):
        await listener.run()

    record_feedback.assert_awaited_once_with(
        peer="+15551234567",
        emoji="👍",
        target_sent_timestamp=1_716_800_000_000,
    )
    graph.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_reaction_to_non_bot_message_dropped() -> None:
    """Reactions to messages not authored by the bot must not record feedback."""
    from app.ingress.signal_listener import SignalListener

    graph = AsyncMock()
    listener = SignalListener(
        graph=graph,
        account="+15559876543",
        authorized_peers=frozenset({"+15551234567"}),
    )

    record_feedback = AsyncMock(return_value=True)
    envelopes = [
        _reaction_envelope(
            "+15551234567",
            emoji="👍",
            target_author="+15550000000",
        )
    ]
    with (
        patch("app.ingress.signal_listener.receive_messages", return_value=_async_gen(envelopes)),
        patch("app.tools.rewards.record_reward_feedback", new=record_feedback),
    ):
        await listener.run()

    record_feedback.assert_not_awaited()
    graph.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_unauthorized_reaction_dropped_before_feedback_handler() -> None:
    """Unauthorized reactions must not reach record_reward_feedback."""
    from app.ingress.signal_listener import SignalListener

    graph = AsyncMock()
    listener = SignalListener(
        graph=graph,
        authorized_peers=frozenset({"+15551234567"}),
    )

    record_feedback = AsyncMock(return_value=True)
    envelopes = [_reaction_envelope("+19990001111", emoji="👍")]
    with (
        patch("app.ingress.signal_listener.receive_messages", return_value=_async_gen(envelopes)),
        patch("app.tools.rewards.record_reward_feedback", new=record_feedback),
    ):
        await listener.run()

    record_feedback.assert_not_awaited()
    graph.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_stream_only_authorized_reaches_graph() -> None:
    """Among interleaved peers, only the authorized one reaches the graph."""
    from app.ingress.signal_listener import SignalListener

    graph = AsyncMock()
    listener = SignalListener(
        graph=graph,
        authorized_peers=frozenset({"+15551234567"}),
    )

    envelopes = [
        _envelope("+19990001111", "attacker probing"),
        _envelope("+15551234567", "legit message"),
        _envelope("+19990002222", "another attacker"),
    ]
    with patch(
        "app.ingress.signal_listener.receive_messages",
        return_value=_async_gen(envelopes),
    ):
        await listener.run()

    assert graph.ainvoke.await_count == 1
    args, _ = graph.ainvoke.await_args
    assert args[0]["peer"] == "+15551234567"
