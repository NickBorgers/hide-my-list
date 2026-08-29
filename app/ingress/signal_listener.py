"""Signal inbound message listener.

Consumes the signal-cli-rest-api WebSocket stream and routes each
(peer, text) pair through the LangGraph pipeline.

Authorization: AUTHORIZED_PEERS is a comma-separated env var of allowed
E.164 numbers. Messages from any other peer are silently dropped — no
reply is sent so the channel reveals no signal-cli liveness to an
attacker who happened to discover the bot's number. An empty or unset
AUTHORIZED_PEERS refuses startup; the fail-safe default is closed.

This module is one of three authorised sites for httpx.AsyncClient usage.
"""
from __future__ import annotations

import asyncio
import os
from collections import deque
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

import structlog

from app.tools.signal_client import (
    receive_messages,
    send_message,
    send_read_receipt,
    send_typing_indicator,
)
from app.tools.signal_ingress_health import record_inbound_message

log = structlog.get_logger(__name__)

_TYPING_REFRESH_SECONDS = 10.0
_MESSAGE_DEBOUNCE_SECONDS = 3.0
_DEFAULT_QUEUE_DEPTH = 32
_OVERFLOW_REPLY = (
    "I'm catching up and can't take more messages right now. Please try again in a minute."
)


@dataclass(frozen=True)
class _QueuedMessage:
    peer: str
    text: str


def _load_authorized_peers() -> frozenset[str]:
    """Read AUTHORIZED_PEERS env var; return the frozenset of E.164 strings.

    Raises RuntimeError if the env var is missing or yields no usable peers
    after parsing — open ingress against single-tenant Notion data is not a
    default we'll ship.
    """
    raw = os.environ.get("AUTHORIZED_PEERS", "")
    peers = frozenset(p.strip() for p in raw.split(",") if p.strip())
    if not peers:
        raise RuntimeError(
            "AUTHORIZED_PEERS is empty or unset. Refusing to start: any peer "
            "that knows the signal-cli account number could otherwise read "
            "tasks from the single-tenant Notion database. Set "
            "AUTHORIZED_PEERS to a comma-separated list of E.164 numbers."
        )
    return peers


def _extract_peer_and_text(envelope: dict[str, Any]) -> tuple[str, str, int | None] | None:
    """Extract (sender_e164, text, timestamp) from a signal-cli envelope dict.

    Returns None if the envelope is not a text message from a peer.
    """
    outer = envelope.get("envelope", {})
    data_message = outer.get("dataMessage", {})
    text = data_message.get("message", "")
    if not text:
        return None

    source = outer.get("source", "")
    if not source:
        return None

    timestamp = outer.get("timestamp")
    if not isinstance(timestamp, int):
        timestamp = data_message.get("timestamp")
    if not isinstance(timestamp, int):
        timestamp = None

    return source, text, timestamp


def _log_background_task_result(task: asyncio.Task[None]) -> None:
    """Consume unexpected background task exceptions without leaking content."""
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        log.warning("signal_listener.background_task_failed", error_type=type(exc).__name__)


def _start_background_task(coro: Coroutine[Any, Any, None]) -> None:
    """Schedule a best-effort coroutine and log unexpected failures."""
    task = asyncio.create_task(coro)
    task.add_done_callback(_log_background_task_result)


async def _record_inbound_activity() -> None:
    """Best-effort durable marker that authorized Signal ingress is alive."""
    try:
        await record_inbound_message()
    except Exception as exc:
        log.warning(
            "signal_listener.ingress_health_record_failed",
            error_type=type(exc).__name__,
        )


async def _maintain_typing_indicator(
    *,
    peer: str,
    stop_event: asyncio.Event,
    base_url: str | None,
    account: str | None,
    refresh_seconds: float = _TYPING_REFRESH_SECONDS,
) -> None:
    """Refresh Signal typing indicator until stop_event is set."""
    while not stop_event.is_set():
        await send_typing_indicator(
            peer,
            started=True,
            base_url=base_url,
            account=account,
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=refresh_seconds)
        except TimeoutError:
            continue


class _InboundMessageBuffer:
    """Bounded async buffer for text messages waiting on the serial graph worker."""

    def __init__(self, max_depth: int) -> None:
        if max_depth <= 0:
            raise ValueError("max_depth must be positive")
        self._max_depth = max_depth
        self._pending: deque[_QueuedMessage] = deque()
        self._closed = False
        self._condition = asyncio.Condition()

    @property
    def max_depth(self) -> int:
        return self._max_depth

    async def try_add(self, message: _QueuedMessage) -> bool:
        """Add a message if capacity remains; return False on overflow."""
        async with self._condition:
            if self._closed or len(self._pending) >= self._max_depth:
                return False
            self._pending.append(message)
            self._condition.notify()
            return True

    async def get(self) -> _QueuedMessage | None:
        """Pop the next message, or None once the buffer is closed and empty."""
        async with self._condition:
            while not self._pending and not self._closed:
                await self._condition.wait()
            if not self._pending:
                return None
            return self._pending.popleft()

    async def collect_peer(self, peer: str) -> list[_QueuedMessage]:
        """Remove all currently pending messages from one peer, preserving order."""
        async with self._condition:
            matching: list[_QueuedMessage] = []
            remaining: deque[_QueuedMessage] = deque()
            while self._pending:
                message = self._pending.popleft()
                if message.peer == peer:
                    matching.append(message)
                else:
                    remaining.append(message)
            self._pending = remaining
            return matching

    async def close(self) -> None:
        """Stop accepting messages and let the worker drain what remains."""
        async with self._condition:
            self._closed = True
            self._condition.notify_all()


def _extract_reaction(envelope: dict[str, Any]) -> tuple[str, str, int, str] | None:
    """Extract reaction feedback fields from a signal-cli envelope.

    Returns None for non-reaction envelopes, removed reactions, or malformed
    reaction payloads.
    """
    outer = envelope.get("envelope", {})
    data_message = outer.get("dataMessage", {})
    reaction = data_message.get("reaction")
    if not isinstance(reaction, dict):
        return None

    if reaction.get("isRemove") is True:
        return None

    source = outer.get("source", "")
    emoji = reaction.get("emoji", "")
    target_author = reaction.get("targetAuthor", "")
    target_sent_timestamp = reaction.get("targetSentTimestamp")
    if (
        not source
        or not emoji
        or not target_author
        or not isinstance(target_sent_timestamp, int)
    ):
        return None

    return source, emoji, target_sent_timestamp, target_author


def _target_author_matches_account(target_author: str, account: str | None) -> bool:
    """Return whether a reaction targeted a bot-authored message."""
    expected_account = account or os.environ.get("SIGNAL_ACCOUNT", "")
    return bool(expected_account) and target_author == expected_account


class SignalListener:
    """Asyncio task that consumes signal-cli WebSocket and drives the graph."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        account: str | None = None,
        graph: Any = None,
        authorized_peers: frozenset[str] | None = None,
        message_debounce_seconds: float = _MESSAGE_DEBOUNCE_SECONDS,
        queue_depth: int = _DEFAULT_QUEUE_DEPTH,
    ) -> None:
        self._base_url = base_url
        self._account = account
        self._graph = graph  # injected in tests; built lazily in production
        self._message_debounce_seconds = message_debounce_seconds
        self._message_buffer = _InboundMessageBuffer(queue_depth)
        # Eagerly load on construction so a misconfiguration fails fast at
        # startup instead of at the first inbound message.
        self._authorized_peers = (
            authorized_peers if authorized_peers is not None
            else _load_authorized_peers()
        )

    def _get_graph(self) -> Any:
        if self._graph is not None:
            return self._graph
        # Lazy import to avoid circular deps at module load
        from app.graph.graph import build_graph
        return build_graph()

    async def _send_overflow_reply(self, peer: str) -> None:
        """Tell the authorized sender that this message could not be queued."""
        try:
            await send_message(
                peer,
                _OVERFLOW_REPLY,
                base_url=self._base_url,
                account=self._account,
            )
        except Exception as exc:
            log.warning(
                "signal_listener.queue_overflow_reply_failed",
                error_type=type(exc).__name__,
            )

    async def _invoke_graph_for_messages(
        self,
        *,
        graph: Any,
        peer: str,
        messages: list[_QueuedMessage],
    ) -> None:
        incoming = "\n".join(message.text for message in messages)
        typing_stop = asyncio.Event()
        _start_background_task(
            _maintain_typing_indicator(
                peer=peer,
                stop_event=typing_stop,
                base_url=self._base_url,
                account=self._account,
            )
        )
        try:
            await graph.ainvoke(
                {"peer": peer, "incoming": incoming},
                config={"configurable": {"thread_id": peer}},
            )
            if len(messages) > 1:
                log.info(
                    "signal_listener.messages_coalesced",
                    peer="<recipient>",
                    message_count=len(messages),
                )
        except Exception:
            log.exception("signal_listener.graph_error", peer="<recipient>")
        finally:
            typing_stop.set()
            _start_background_task(
                send_typing_indicator(
                    peer,
                    started=False,
                    base_url=self._base_url,
                    account=self._account,
                )
            )

    async def _process_messages(self) -> None:
        """Drain queued text messages serially, coalescing rapid same-peer sends."""
        graph: Any | None = None
        while True:
            first = await self._message_buffer.get()
            if first is None:
                return

            if self._message_debounce_seconds > 0:
                await asyncio.sleep(self._message_debounce_seconds)

            messages = [first]
            messages.extend(await self._message_buffer.collect_peer(first.peer))

            if graph is None:
                graph = self._get_graph()
            await self._invoke_graph_for_messages(
                graph=graph,
                peer=first.peer,
                messages=messages,
            )

    async def run(self) -> None:
        """Main loop: consume WebSocket, route each message to the graph."""
        log.info(
            "signal_listener.started",
            authorized_peer_count=len(self._authorized_peers),
        )
        worker = asyncio.create_task(self._process_messages())
        worker.add_done_callback(_log_background_task_result)

        try:
            async for envelope in receive_messages(
                base_url=self._base_url,
                account=self._account,
            ):
                reaction = _extract_reaction(envelope)
                if reaction is not None:
                    peer, emoji, target_sent_timestamp, target_author = reaction
                    if peer not in self._authorized_peers:
                        log.warning("signal_listener.unauthorized_peer_dropped")
                        continue

                    if not _target_author_matches_account(target_author, self._account):
                        log.info("signal_listener.reaction_non_bot_target_dropped")
                        continue

                    await _record_inbound_activity()

                    from app.tools.rewards import record_reward_feedback

                    await record_reward_feedback(
                        peer=peer,
                        emoji=emoji,
                        target_sent_timestamp=target_sent_timestamp,
                    )
                    log.info("signal_listener.reaction_recorded")
                    continue

                result = _extract_peer_and_text(envelope)
                if result is None:
                    continue

                peer, text, timestamp = result

                if peer not in self._authorized_peers:
                    log.warning("signal_listener.unauthorized_peer_dropped")
                    continue

                await _record_inbound_activity()

                log.info("signal_listener.message_received", peer="<recipient>")

                if timestamp is not None:
                    _start_background_task(
                        send_read_receipt(
                            peer,
                            timestamp,
                            base_url=self._base_url,
                            account=self._account,
                        )
                    )
                else:
                    log.warning("signal_listener.receipt_skipped_missing_timestamp")

                queued = await self._message_buffer.try_add(
                    _QueuedMessage(peer=peer, text=text)
                )
                if queued:
                    continue

                log.warning(
                    "signal_listener.queue_overflow",
                    peer="<recipient>",
                    queue_depth=self._message_buffer.max_depth,
                )
                _start_background_task(
                    self._send_overflow_reply(peer)
                )
        except asyncio.CancelledError:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            raise
        except Exception:
            await self._message_buffer.close()
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            raise
        else:
            await self._message_buffer.close()
            await worker
