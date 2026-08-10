"""Signal CLI REST API client.

Async HTTP + WebSocket client wrapping bbernhard/signal-cli-rest-api.
Provides outbound message sends, read receipts, typing indicators, and
inbound message streaming via WebSocket.

This module is one of three authorised sites for httpx.AsyncClient usage
(alongside app/tools/notion.py and app/ingress/signal_listener.py).

Attachment policy (PNG only):
- Only .png files are accepted (case-insensitive). Any other extension raises
  ValueError immediately. This enforces the user's content-type allowlist policy
  and keeps the narrow-tool-surface invariant.
- Paths must be absolute and must not contain '..'.
- Paths must resolve under the reward_artifacts root (REWARD_ARTIFACTS_DIR env,
  defaulting to /tmp/reward_artifacts). Any path outside that root raises ValueError.
- File bytes are base64-encoded and passed as base64_attachments in the /v2/send
  JSON body per the bbernhard/signal-cli-rest-api spec (raw base64 strings, not
  data-URL form — the v2 API expects plain base64).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import structlog
import websockets
import websockets.exceptions

log = structlog.get_logger(__name__)

_DEFAULT_BASE_URL = "http://localhost:8080"
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 60.0
_MAX_RETRIES = 5
_RETRY_BASE_DELAY = 1.0  # seconds; exponential backoff
_DEFAULT_RECEIVE_IDLE_TIMEOUT_SECONDS = 300.0


class _ReceiveIdleTimeoutError(Exception):
    """Raised when the receive WebSocket stays open without delivering frames."""


def _signal_base_url() -> str:
    return os.environ.get("SIGNAL_CLI_URL", _DEFAULT_BASE_URL)


def _account() -> str:
    return os.environ["SIGNAL_ACCOUNT"]


def _receive_idle_timeout_seconds() -> float:
    raw = os.environ.get(
        "SIGNAL_RECEIVE_IDLE_TIMEOUT_SECONDS",
        str(_DEFAULT_RECEIVE_IDLE_TIMEOUT_SECONDS),
    )
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "signal_client.receive.invalid_idle_timeout",
            configured_value=raw,
            fallback_seconds=_DEFAULT_RECEIVE_IDLE_TIMEOUT_SECONDS,
        )
        return _DEFAULT_RECEIVE_IDLE_TIMEOUT_SECONDS
    if value <= 0:
        log.warning(
            "signal_client.receive.invalid_idle_timeout",
            configured_value=raw,
            fallback_seconds=_DEFAULT_RECEIVE_IDLE_TIMEOUT_SECONDS,
        )
        return _DEFAULT_RECEIVE_IDLE_TIMEOUT_SECONDS
    return value


# ---------------------------------------------------------------------------
# Send / UX signals
# ---------------------------------------------------------------------------


def _reward_artifacts_root() -> Path:
    """Return the canonical reward artifacts root as a resolved absolute Path."""
    raw = os.environ.get("REWARD_ARTIFACTS_DIR", "/tmp/reward_artifacts")
    return Path(raw).resolve()


def _validate_attachment_path(path_str: str) -> Path:
    """Validate and return a resolved Path for a PNG attachment.

    Enforces:
    - Path must be absolute.
    - Path must not contain '..'.
    - Extension must be .png (case-insensitive). PNG-only allowlist per
      user's content-type policy.
    - Resolved path must exist.
    - Resolved path must be under the reward_artifacts root.

    Args:
        path_str: Filesystem path string supplied by the caller.

    Returns:
        Resolved absolute Path to the PNG file.

    Raises:
        ValueError: For any policy violation (non-absolute, traversal, non-PNG,
            missing file, or outside reward_artifacts root).
    """
    # Reject relative paths and traversal sequences before resolving.
    if not os.path.isabs(path_str):
        raise ValueError("Attachment path must be absolute")
    if ".." in Path(path_str).parts:
        raise ValueError("Attachment path must not contain '..'")

    resolved = Path(path_str).resolve()

    # PNG-only content-type allowlist.
    if resolved.suffix.lower() != ".png":
        raise ValueError("Attachment must be a .png file")

    # File must exist.
    if not resolved.exists():
        raise ValueError("Attachment path does not exist")

    # Resolved path must be under the reward_artifacts root.
    artifacts_root = _reward_artifacts_root()
    try:
        resolved.relative_to(artifacts_root)
    except ValueError:
        raise ValueError(
            "Attachment path is outside the reward_artifacts root"
        ) from None

    return resolved


async def send_message(
    recipient: str,
    message: str,
    *,
    attachment_paths: list[str] | None = None,
    idempotency_key: str | None = None,
    base_url: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Send a Signal message to recipient via signal-cli REST API.

    Args:
        recipient: E.164 phone number of the recipient.
        message: Text body to send.
        attachment_paths: Optional list of absolute paths to PNG files to attach.
            Each path is validated (must be absolute, no '..', .png only, must
            exist, must be under the reward_artifacts root). Bytes are base64-
            encoded and sent as base64_attachments in the /v2/send JSON body.
            Raises ValueError on any path violation — fail-fast before HTTP.
        idempotency_key: Unique string for operator-level duplicate detection.
            Signal-cli REST API does not natively deduplicate sends by key, but
            storing and logging the key enables tracing when duplicates occur.
        base_url: Override for SIGNAL_CLI_URL (used in tests).
        account: Override for SIGNAL_ACCOUNT (used in tests).

    Returns:
        Parsed JSON response from signal-cli.

    Raises:
        ValueError: If any attachment path fails validation.
    """
    url_base = base_url or _signal_base_url()
    acct = account or _account()

    payload: dict[str, Any] = {
        "message": message,
        "number": acct,
        "recipients": [recipient],
    }

    # Validate and encode attachments — fail fast before any HTTP call.
    if attachment_paths:
        encoded: list[str] = []
        for path_str in attachment_paths:
            resolved = _validate_attachment_path(path_str)
            b64 = base64.b64encode(resolved.read_bytes()).decode("ascii")
            encoded.append(b64)
        payload["base64_attachments"] = encoded

    # Log send attempt for operator-level tracing and duplicate detection.
    # Signal-cli REST API does not support client-side deduplication;
    # the key enables tracing and reconciliation when duplicates occur.
    log.info(
        "signal_client.send",
        idempotency_key=idempotency_key,
        attachment_count=len(attachment_paths) if attachment_paths else 0,
    )

    async with httpx.AsyncClient(
        base_url=url_base,
        timeout=httpx.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT, write=30.0, pool=10.0),
    ) as client:
        delay = _RETRY_BASE_DELAY
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await client.post("/v2/send", json=payload)
                resp.raise_for_status()
                result = resp.json()
                log.info(
                    "signal_client.send.ok",
                    signal_timestamp=result.get("timestamp"),
                )
                return result  # type: ignore[no-any-return]
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    raise
                log.warning(
                    "signal_client.send.retry",
                    attempt=attempt + 1,
                    error=str(exc),
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
        raise RuntimeError(f"send_message failed after {_MAX_RETRIES} attempts") from last_exc


async def _best_effort_signal_request(
    request: Callable[[], Awaitable[httpx.Response]],
    *,
    event: str,
) -> None:
    """Run a signal-cli request with send_message retry timing, never raising."""
    delay = _RETRY_BASE_DELAY
    for attempt in range(_MAX_RETRIES):
        try:
            resp = await request()
            resp.raise_for_status()
            log.info(f"{event}.ok")
            return
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            retryable = not (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code < 500
            )
            log.warning(
                f"{event}.failed" if not retryable else f"{event}.retry",
                attempt=attempt + 1,
                error_type=type(exc).__name__,
            )
            if not retryable or attempt == _MAX_RETRIES - 1:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
        except Exception as exc:
            log.warning(f"{event}.failed", error_type=type(exc).__name__)
            return


async def send_read_receipt(
    peer: str,
    timestamp: int,
    *,
    base_url: str | None = None,
    account: str | None = None,
) -> None:
    """Best-effort Signal read receipt for a received peer message."""
    try:
        url_base = base_url or _signal_base_url()
        acct = account or _account()
    except Exception as exc:
        log.warning("signal_client.receipt.failed", error_type=type(exc).__name__)
        return

    payload = {
        "recipient": peer,
        "receipt_type": "read",
        "timestamp": timestamp,
    }

    async with httpx.AsyncClient(
        base_url=url_base,
        timeout=httpx.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT, write=30.0, pool=10.0),
    ) as client:
        await _best_effort_signal_request(
            lambda: client.post(f"/v1/receipts/{acct}", json=payload),
            event="signal_client.receipt",
        )


async def send_typing_indicator(
    peer: str,
    *,
    started: bool = True,
    base_url: str | None = None,
    account: str | None = None,
) -> None:
    """Best-effort Signal typing indicator start/stop for a peer."""
    try:
        url_base = base_url or _signal_base_url()
        acct = account or _account()
    except Exception as exc:
        log.warning("signal_client.typing.failed", error_type=type(exc).__name__)
        return

    payload = {"recipient": peer}
    async with httpx.AsyncClient(
        base_url=url_base,
        timeout=httpx.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT, write=30.0, pool=10.0),
    ) as client:
        await _best_effort_signal_request(
            lambda: client.request(
                "PUT" if started else "DELETE",
                f"/v1/typing-indicator/{acct}",
                json=payload,
            ),
            event="signal_client.typing.start" if started else "signal_client.typing.stop",
        )


# ---------------------------------------------------------------------------
# Receive — WebSocket consumer
# ---------------------------------------------------------------------------


async def receive_messages(
    *,
    base_url: str | None = None,
    account: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Async generator yielding inbound message dicts from signal-cli WebSocket.

    Each yielded item is the parsed JSON envelope from signal-cli.
    Reconnects on WebSocket errors with exponential backoff.

    Args:
        base_url: Override for SIGNAL_CLI_URL (used in tests).
        account: Override for SIGNAL_ACCOUNT (used in tests).
    """
    url_base = base_url or _signal_base_url()
    acct = account or _account()

    # Build WebSocket URL: http://... -> ws://..., https://... -> wss://...
    ws_url = url_base.replace("https://", "wss://").replace("http://", "ws://")
    ws_endpoint = f"{ws_url}/v1/receive/{acct}"
    idle_timeout_seconds = _receive_idle_timeout_seconds()

    delay = _RETRY_BASE_DELAY
    while True:
        try:
            async with websockets.connect(ws_endpoint) as ws:
                delay = _RETRY_BASE_DELAY  # reset on successful connect
                message_stream = aiter(ws)
                while True:
                    try:
                        raw_message = await asyncio.wait_for(
                            anext(message_stream),
                            timeout=idle_timeout_seconds,
                        )
                    except StopAsyncIteration:
                        log.warning("signal_client.receive.closed", delay=delay)
                        break
                    except TimeoutError as exc:
                        log.warning(
                            "signal_client.receive.idle_timeout",
                            idle_timeout_seconds=idle_timeout_seconds,
                        )
                        raise _ReceiveIdleTimeoutError(
                            f"receive stream idle for {idle_timeout_seconds} seconds"
                        ) from exc
                    if not raw_message:
                        continue
                    try:
                        yield json.loads(raw_message)
                    except json.JSONDecodeError:
                        log.warning("signal_client.receive.bad_json")
        except (
            _ReceiveIdleTimeoutError,
            websockets.exceptions.WebSocketException,
            OSError,
        ) as exc:
            log.warning("signal_client.receive.reconnect", error=str(exc), delay=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
