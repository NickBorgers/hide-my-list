"""Signal outbound capture sink.

Replaces every path that would put bytes on the wire toward signal-cli and
records what was sent instead. Two details carry weight:

`send_message`'s signature is pinned against the real
`app.tools.signal_client.send_message` by a unit test. A sink that accepts
`**kwargs` would keep passing after a caller started sending an argument the
real client does not take — the mock-drift failure the test rig calls bug
class 10.

The returned `timestamp` is monotonic and deterministic. Both `send_node` and
`reminder_worker` read `result["timestamp"]`, and the worker writes it into
`recent_outbound.signal_timestamp` — the key a later COMPLETE turn resolves
against. Non-deterministic timestamps there would make that row unassertable.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Fixed epoch-ms base so captured timestamps are stable across runs and read
# as obviously synthetic in a failure dump.
_TIMESTAMP_BASE = 1_700_000_000_000


@dataclass(frozen=True)
class SentMessage:
    """One captured outbound Signal message."""

    recipient: str
    body: str
    idempotency_key: str | None
    attachment_paths: tuple[str, ...]
    timestamp: int


class SignalSink:
    """Captures outbound Signal traffic in call order."""

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.read_receipts: list[tuple[str, int]] = []
        self.typing_events: list[tuple[str, bool]] = []
        self._timestamp = _TIMESTAMP_BASE

    def _next_timestamp(self) -> int:
        self._timestamp += 1
        return self._timestamp

    def mark(self) -> int:
        """Return a cursor into `sent` so a caller can slice one turn's sends."""
        return len(self.sent)

    def since(self, cursor: int) -> list[SentMessage]:
        return self.sent[cursor:]

    async def send_message(
        self,
        recipient: str,
        message: str,
        *,
        attachment_paths: list[str] | None = None,
        idempotency_key: str | None = None,
        base_url: str | None = None,
        account: str | None = None,
    ) -> dict[str, Any]:
        timestamp = self._next_timestamp()
        self.sent.append(
            SentMessage(
                recipient=recipient,
                body=message,
                idempotency_key=idempotency_key,
                attachment_paths=tuple(attachment_paths or ()),
                timestamp=timestamp,
            )
        )
        return {"timestamp": timestamp}

    async def send_read_receipt(
        self,
        peer: str,
        timestamp: int,
        *,
        base_url: str | None = None,
        account: str | None = None,
    ) -> None:
        self.read_receipts.append((peer, timestamp))

    async def send_typing_indicator(
        self,
        peer: str,
        *,
        started: bool = True,
        base_url: str | None = None,
        account: str | None = None,
    ) -> None:
        self.typing_events.append((peer, started))

    def install(self) -> Callable[[], None]:
        """Patch every Signal egress path. Returns an undo callable.

        Four targets, not one. `send_node` and `reminder_worker` look
        `send_message` up on the module at call time, so patching
        `app.tools.signal_client` covers them. The ingress listener instead does
        module-level imports for the overflow send and receipt/typing helpers,
        so its own namespace holds the references those calls resolve.
        """
        from app.ingress import signal_listener
        from app.tools import signal_client

        targets: list[tuple[Any, str, Any]] = [
            (signal_client, "send_message", self.send_message),
            (signal_listener, "send_message", self.send_message),
            (signal_listener, "send_read_receipt", self.send_read_receipt),
            (signal_listener, "send_typing_indicator", self.send_typing_indicator),
        ]
        original = [(mod, name, getattr(mod, name)) for mod, name, _ in targets]
        for mod, name, replacement in targets:
            setattr(mod, name, replacement)

        def _undo() -> None:
            for mod, name, fn in original:
                setattr(mod, name, fn)

        return _undo
