"""Operational alerts: enqueue and drain.

Ops alerts are internal operational notifications sent to the operator's
Signal number (OPS_ALERT_SIGNAL_NUMBER env var). They are NOT user-facing
content — do not include private user data (task titles, reminder content,
phone numbers, real names) in alert bodies. Use placeholders like <page_id>.

Flow:
  1. enqueue(kind, body, severity) — inserts a pending row into ops_alerts.
  2. drain() — called by the ops_alerts_drain APScheduler job every 5 min.
              Sends pending alerts via signal_client, marks delivered.
              Respects per-kind throttle via ops_alerts_throttle table.

Throttle: an alert_kind is suppressed for THROTTLE_SECONDS after the last
delivered alert of that kind. This prevents alert storms.

At-least-once delivery: if drain() crashes after sending but before marking
delivered, the same alert will be sent again on the next drain cycle.
Duplicate ops alerts are acceptable; silently dropping them is not.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog

from app.tools.db import get_db_conn

log = structlog.get_logger(__name__)

# Throttle: same alert_kind won't be re-sent within this window.
THROTTLE_SECONDS = 3600  # 1 hour

Severity = Literal["info", "warning", "critical"]


def _now() -> datetime:
    return datetime.now(UTC)


def _ops_alert_recipient() -> str:
    """Return the operator Signal number from env. Raises if missing."""
    number = os.environ.get("OPS_ALERT_SIGNAL_NUMBER", "")
    if not number:
        raise RuntimeError(
            "OPS_ALERT_SIGNAL_NUMBER env var not set; cannot deliver ops alerts"
        )
    return number


async def enqueue(kind: str, body: str, severity: Severity = "warning") -> uuid.UUID:
    """Insert a pending ops alert into the database.

    Idempotent in the sense that duplicate enqueues create additional rows
    (not deduplicated by kind), but drain() throttles delivery per kind.

    Args:
        kind: Short identifier for throttle lookup, e.g. 'notion_health_failed'.
              Must not exceed 255 chars. Alphanumeric + underscores preferred.
        body: Human-readable alert text. Must not contain private user data.
        severity: 'info', 'warning', or 'critical'.

    Returns:
        The UUID of the inserted alert row.
    """
    alert_id = uuid.uuid4()
    async with get_db_conn() as conn:
        await conn.execute(
            """
            INSERT INTO ops_alerts (id, alert_kind, body, severity, state, created_at)
            VALUES (%s, %s, %s, %s, 'pending', %s)
            """,
            (alert_id, kind, body, severity, _now()),
        )
    log.info("ops_alerts.enqueued", alert_id=str(alert_id), kind=kind, severity=severity)
    return alert_id


async def drain() -> None:
    """Fetch pending ops alerts and deliver via signal_client.

    Respects per-kind throttle. Marks delivered alerts. Logs failures but
    does not re-raise so the APScheduler job stays alive.

    Called by the ops_alerts_drain APScheduler job every 5 minutes.
    """
    from app.tools import signal_client  # local import avoids circular at module load

    try:
        recipient = _ops_alert_recipient()
    except RuntimeError as exc:
        log.error("ops_alerts.drain.no_recipient", error=str(exc))
        return

    async with get_db_conn() as conn:
        # Fetch all pending alerts, oldest first.
        rows = await conn.execute(
            """
            SELECT id, alert_kind, body, severity, created_at
            FROM ops_alerts
            WHERE state = 'pending'
            ORDER BY created_at ASC
            """,
        )
        alerts = await rows.fetchall()

    for alert in alerts:
        alert_id: uuid.UUID = alert["id"]
        kind: str = alert["alert_kind"]
        body: str = alert["body"]
        severity: str = alert["severity"]

        # Check throttle: skip if this kind was delivered recently.
        throttled = await _is_throttled(kind)
        if throttled:
            log.info(
                "ops_alerts.drain.throttled",
                alert_id=str(alert_id),
                kind=kind,
            )
            async with get_db_conn() as conn:
                await conn.execute(
                    "UPDATE ops_alerts SET state = 'throttled' WHERE id = %s",
                    (alert_id,),
                )
            continue

        # Send via signal_client.
        message = f"[{severity.upper()}] {body}"
        try:
            await signal_client.send_message(recipient=recipient, message=message)
            delivered_at = _now()

            async with get_db_conn() as conn:
                await conn.execute(
                    """
                    UPDATE ops_alerts
                    SET state = 'delivered', delivered_at = %s
                    WHERE id = %s
                    """,
                    (delivered_at, alert_id),
                )
                # Update throttle table (upsert).
                await conn.execute(
                    """
                    INSERT INTO ops_alerts_throttle (alert_kind, last_sent_at)
                    VALUES (%s, %s)
                    ON CONFLICT (alert_kind)
                    DO UPDATE SET last_sent_at = EXCLUDED.last_sent_at
                    """,
                    (kind, delivered_at),
                )

            log.info(
                "ops_alerts.drain.delivered",
                alert_id=str(alert_id),
                kind=kind,
                severity=severity,
            )

        except Exception as exc:
            log.error(
                "ops_alerts.drain.send_failed",
                alert_id=str(alert_id),
                kind=kind,
                error=str(exc),
            )
            # Leave state = 'pending' so the next drain cycle retries.
            # Record the error for diagnostics but do NOT mark 'failed' —
            # that would permanently suppress this alert (drain only selects pending).
            # At-least-once contract: transient failures must not drop alerts.
            async with get_db_conn() as conn:
                await conn.execute(
                    "UPDATE ops_alerts SET error = %s WHERE id = %s AND state = 'pending'",
                    (str(exc), alert_id),
                )


async def _is_throttled(kind: str) -> bool:
    """Return True if the given alert kind was delivered within THROTTLE_SECONDS."""
    async with get_db_conn() as conn:
        row = await conn.execute(
            "SELECT last_sent_at FROM ops_alerts_throttle WHERE alert_kind = %s",
            (kind,),
        )
        result = await row.fetchone()

    if result is None:
        return False

    last_sent_at: datetime = result["last_sent_at"]
    cutoff = _now() - timedelta(seconds=THROTTLE_SECONDS)
    return last_sent_at > cutoff
