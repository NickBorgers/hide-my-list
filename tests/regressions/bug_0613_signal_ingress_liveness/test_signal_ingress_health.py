from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog.testing


class _Cursor:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    async def fetchone(self) -> dict[str, Any] | None:
        return self._row


class _Conn:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    async def execute(self, _query: str, _params: tuple[Any, ...]) -> _Cursor:
        return _Cursor(self._row)


def _db_conn_for(row: dict[str, Any] | None):
    @asynccontextmanager
    async def _db_conn() -> AsyncIterator[_Conn]:
        yield _Conn(row)

    return _db_conn


@pytest.mark.asyncio
async def test_silence_detector_stays_quiet_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools import ops_alerts, signal_ingress_health

    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    row = {"last_inbound_at": now - timedelta(hours=12)}
    enqueue = AsyncMock()

    monkeypatch.setenv("SIGNAL_INBOUND_SILENCE_ALERT_THRESHOLD_SECONDS", "86400")
    monkeypatch.setattr(signal_ingress_health, "get_db_conn", _db_conn_for(row))
    monkeypatch.setattr(ops_alerts, "enqueue", enqueue)

    alerted = await signal_ingress_health.check_inbound_silence(now=now)

    assert alerted is False
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_silence_detector_logs_but_does_not_alert_past_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools import ops_alerts, signal_ingress_health

    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    row = {"last_inbound_at": now - timedelta(hours=49)}
    enqueue = AsyncMock()

    monkeypatch.setenv("SIGNAL_INBOUND_SILENCE_ALERT_THRESHOLD_SECONDS", "86400")
    monkeypatch.setattr(signal_ingress_health, "get_db_conn", _db_conn_for(row))
    monkeypatch.setattr(ops_alerts, "enqueue", enqueue)

    # Silence past the threshold is expected on low-traffic instances, so it is
    # logged rather than paged: the detector reports True but never enqueues.
    with structlog.testing.capture_logs() as logs:
        alerted = await signal_ingress_health.check_inbound_silence(now=now)

    assert alerted is True
    enqueue.assert_not_awaited()
    silent_events = [e for e in logs if e.get("event") == "signal_ingress_health.silent"]
    assert len(silent_events) == 1, "expected exactly one signal_ingress_health.silent log event"
    assert "silence_seconds" in silent_events[0]
    assert "threshold_seconds" in silent_events[0]
    assert "last_inbound_at" in silent_events[0]


@pytest.mark.asyncio
async def test_silence_detector_does_not_alert_when_marker_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools import ops_alerts, signal_ingress_health

    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    enqueue = AsyncMock()

    monkeypatch.setattr(signal_ingress_health, "get_db_conn", _db_conn_for(None))
    monkeypatch.setattr(ops_alerts, "enqueue", enqueue)

    with structlog.testing.capture_logs() as logs:
        alerted = await signal_ingress_health.check_inbound_silence(now=now)

    assert alerted is True
    enqueue.assert_not_awaited()
    missing_events = [e for e in logs if e.get("event") == "signal_ingress_health.missing_marker"]
    assert len(missing_events) == 1, "expected exactly one signal_ingress_health.missing_marker log event"
