"""Tests for Signal receipt and typing-indicator client primitives."""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _mock_httpx_response(json_data: dict[str, Any] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
async def test_send_read_receipt_posts_read_receipt_payload() -> None:
    from app.tools import signal_client

    captured: dict[str, Any] = {}

    async def fake_post(url: str, *, json: dict[str, Any]) -> MagicMock:
        captured["url"] = url
        captured["json"] = json
        return _mock_httpx_response()

    mock_client = AsyncMock()
    mock_client.post = fake_post

    with (
        patch.dict(os.environ, {"SIGNAL_ACCOUNT": "<test-account>"}),
        patch("httpx.AsyncClient") as mock_httpx_cls,
    ):
        mock_httpx_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await signal_client.send_read_receipt(
            "<test-recipient>",
            1_716_800_000_000,
            base_url="http://signal-cli-test:8080",
            account="<test-account>",
        )

    assert captured == {
        "url": "/v1/receipts/<test-account>",
        "json": {
            "recipient": "<test-recipient>",
            "receipt_type": "read",
            "timestamp": 1_716_800_000_000,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("started", "method"),
    [
        (True, "PUT"),
        (False, "DELETE"),
    ],
)
async def test_send_typing_indicator_uses_start_stop_methods(
    started: bool,
    method: str,
) -> None:
    from app.tools import signal_client

    captured: dict[str, Any] = {}

    async def fake_request(
        request_method: str,
        url: str,
        *,
        json: dict[str, Any],
    ) -> MagicMock:
        captured["method"] = request_method
        captured["url"] = url
        captured["json"] = json
        return _mock_httpx_response()

    mock_client = AsyncMock()
    mock_client.request = fake_request

    with (
        patch.dict(os.environ, {"SIGNAL_ACCOUNT": "<test-account>"}),
        patch("httpx.AsyncClient") as mock_httpx_cls,
    ):
        mock_httpx_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await signal_client.send_typing_indicator(
            "<test-recipient>",
            started=started,
            base_url="http://signal-cli-test:8080",
            account="<test-account>",
        )

    assert captured == {
        "method": method,
        "url": "/v1/typing-indicator/<test-account>",
        "json": {"recipient": "<test-recipient>"},
    }


@pytest.mark.asyncio
async def test_read_receipt_failure_is_best_effort() -> None:
    from app.tools import signal_client

    async def fake_post(url: str, *, json: dict[str, Any]) -> MagicMock:
        raise httpx.ConnectError("bridge unavailable")

    mock_client = AsyncMock()
    mock_client.post = fake_post

    with (
        patch.dict(os.environ, {"SIGNAL_ACCOUNT": "<test-account>"}),
        patch("asyncio.sleep", new=AsyncMock()),
        patch("httpx.AsyncClient") as mock_httpx_cls,
    ):
        mock_httpx_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await signal_client.send_read_receipt(
            "<test-recipient>",
            1_716_800_000_000,
            base_url="http://signal-cli-test:8080",
            account="<test-account>",
        )
