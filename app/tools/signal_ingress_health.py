"""Durable liveness markers for Signal ingress.

The receive WebSocket can be connected while the product is not receiving
usable inbound traffic. This module stores the last accepted inbound timestamp
in Postgres so scheduler checks survive restarts and crash loops.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import structlog

from app.tools.db import get_db_conn

log = structlog.get_logger(__name__)

_ROW_NAME = "default"
_DEFAULT_INBOUND_SILENCE_THRESHOLD_SECONDS = 36 * 60 * 60


def _now() -> datetime:
    return datetime.now(UTC)


def _silence_threshold_seconds() -> int:
    raw = os.environ.get(
        "SIGNAL_INBOUND_SILENCE_ALERT_THRESHOLD_SECONDS",
        str(_DEFAULT_INBOUND_SILENCE_THRESHOLD_SECONDS),
    )
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            "signal_ingress_health.invalid_silence_threshold",
            configured_value=raw,
            fallback_seconds=_DEFAULT_INBOUND_SILENCE_THRESHOLD_SECONDS,
        )
        return _DEFAULT_INBOUND_SILENCE_THRESHOLD_SECONDS
    if value <= 0:
        log.warning(
            "signal_ingress_health.invalid_silence_threshold",
            configured_value=raw,
            fallback_seconds=_DEFAULT_INBOUND_SILENCE_THRESHOLD_SECONDS,
        )
        return _DEFAULT_INBOUND_SILENCE_THRESHOLD_SECONDS
    return value


async def record_inbound_message(*, received_at: datetime | None = None) -> None:
    """Persist that an authorized inbound Signal item reached the app."""
    timestamp = received_at or _now()
    async with get_db_conn() as conn:
        await conn.execute(
            """
            INSERT INTO signal_ingress_health (name, last_inbound_at, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (name)
            DO UPDATE SET
              last_inbound_at = EXCLUDED.last_inbound_at,
              updated_at = EXCLUDED.updated_at
            """,
            (_ROW_NAME, timestamp, timestamp),
        )
    log.info("signal_ingress_health.recorded")


async def check_inbound_silence(*, now: datetime | None = None) -> bool:
    """Log a warning when Signal ingress has been quiet longer than the threshold.

    Prolonged inbound silence is expected on low-traffic instances — we do not
    send a message every day — so it is recorded as a structured log event rather
    than a paging ops alert. Returns True when silence exceeded the threshold (or
    no durable marker exists) and False otherwise.
    """
    checked_at = now or _now()
    threshold_seconds = _silence_threshold_seconds()
    threshold = timedelta(seconds=threshold_seconds)

    async with get_db_conn() as conn:
        cursor = await conn.execute(
            """
            SELECT last_inbound_at
            FROM signal_ingress_health
            WHERE name = %s
            """,
            (_ROW_NAME,),
        )
        row = await cursor.fetchone()

    if row is None:
        log.warning("signal_ingress_health.missing_marker")
        return True

    last_inbound_at: datetime = row["last_inbound_at"]
    silence_duration = checked_at - last_inbound_at
    if silence_duration <= threshold:
        log.debug(
            "signal_ingress_health.ok",
            silence_seconds=int(silence_duration.total_seconds()),
            threshold_seconds=threshold_seconds,
        )
        return False

    log.warning(
        "signal_ingress_health.silent",
        silence_seconds=int(silence_duration.total_seconds()),
        threshold_seconds=threshold_seconds,
        last_inbound_at=last_inbound_at.isoformat(),
    )
    return True
