"""Reminder delivery worker.

Implements the outbox state machine with SELECT FOR UPDATE SKIP LOCKED,
exponential backoff, dead-lettering, and ops alert throttling.

Delivery contract: at-least-once with idempotency.
The Signal send happens outside the Postgres transaction; if the app crashes
between Signal acceptance and the Postgres commit, the retry will duplicate.

Backoff schedule (capped at 5 attempts):
  attempt 1 -> 1 minute
  attempt 2 -> 5 minutes
  attempt 3 -> 30 minutes
  attempt 4 -> 2 hours
  attempt 5 -> 8 hours
  attempt >= 5 -> dead
"""
from __future__ import annotations

import os
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import psycopg.rows
import structlog

from app.tools import ops_alerts

log = structlog.get_logger(__name__)

_MAX_ATTEMPTS = 5
_BACKOFF_MINUTES = [1, 5, 30, 120, 480]
_LOCK_WINDOW_SECONDS = 120  # locked_until grace window
_OPS_ALERT_THROTTLE_HOURS = 4
_BATCH_SIZE = 10


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _now() -> datetime:
    return datetime.now(UTC)


def _next_due_at(attempt: int) -> datetime:
    """Return the retry due_at for the given attempt number (1-based)."""
    idx = min(attempt - 1, len(_BACKOFF_MINUTES) - 1)
    return _now() + timedelta(minutes=_BACKOFF_MINUTES[idx])


def _coerce_uuid(value: Any) -> uuid.UUID:
    """Accept psycopg-native UUID values and string-like UUID values."""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


async def _throttled_ops_alert(
    conn: psycopg.AsyncConnection[Any],
    alert_kind: str,
    message: str,
) -> None:
    """Enqueue an ops alert, gated by a reminder-worker-side rate check.

    Checks the ops_alerts_throttle table to avoid flooding the queue with
    duplicate dead-reminder alerts. The throttle record is written by
    ops_alerts.drain() only after successful delivery — not here — so that
    drain() can deliver the enqueued alert without immediately re-throttling it.
    """
    async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            "SELECT last_sent_at FROM ops_alerts_throttle WHERE alert_kind = %s",
            (alert_kind,),
        )
        row = await cur.fetchone()

    if row is not None:
        last_sent = row["last_sent_at"]
        if last_sent and (_now() - last_sent) < timedelta(hours=_OPS_ALERT_THROTTLE_HOURS):
            log.debug("ops_alert.throttled", alert_kind=alert_kind)
            return

    # Enqueue for delivery by ops_alerts_drain job; drain writes the throttle record
    # after successful Signal send to avoid suppressing this very alert.
    await ops_alerts.enqueue(kind=alert_kind, body=message, severity="critical")


async def _claim_due_reminders(
    conn: psycopg.AsyncConnection[Any],
    worker_id: str,
) -> list[dict[str, Any]]:
    """SELECT FOR UPDATE SKIP LOCKED claim of due reminders."""
    locked_until = _now() + timedelta(seconds=_LOCK_WINDOW_SECONDS)
    async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            """
            UPDATE reminder_outbox
               SET state = 'delivering',
                   locked_until = %s,
                   worker_id = %s
             WHERE id IN (
               SELECT id FROM reminder_outbox
               WHERE (
                 (state IN ('pending', 'scheduled') AND due_at <= now())
                 OR (state = 'delivering' AND locked_until IS NOT NULL AND locked_until <= now())
               )
               ORDER BY due_at ASC
               LIMIT %s
               FOR UPDATE SKIP LOCKED
             )
            RETURNING *
            """,
            (locked_until, worker_id, _BATCH_SIZE),
        )
        return await cur.fetchall()


async def dispatch_due_reminders(
    conn: psycopg.AsyncConnection[Any],
    *,
    signal_send_fn: Any = None,
) -> None:
    """Claim and deliver all due reminders.

    Args:
        conn: Open async psycopg connection with autocommit=False.
        signal_send_fn: Async callable (recipient, body) -> dict.
            Defaults to app.tools.signal_client.send_message.
            Injected in tests to mock signal-cli.
    """
    if signal_send_fn is None:
        from app.tools.signal_client import send_message
        signal_send_fn = send_message

    worker_id = _worker_id()
    rows = await _claim_due_reminders(conn, worker_id)
    await conn.commit()

    delivered_count = 0
    failed_count = 0

    for row in rows:
        rid = _coerce_uuid(row["id"])
        peer = row["peer"]
        body = row["body"]
        notion_page_id = row["notion_page_id"]
        idempotency_key = row["idempotency_key"]
        attempt = row["attempt"] + 1
        kind = row.get("kind") or "reminder"

        log.info(
            "reminder_worker.delivering",
            reminder_id=str(rid),
            attempt=attempt,
        )

        try:
            # Signal send is outside the Postgres transaction (at-least-once).
            result = await signal_send_fn(
                recipient=peer,
                message=body,
                idempotency_key=idempotency_key,
            )
            signal_ts = result.get("timestamp", 0)

            # Deliver: write signal_timestamp, insert recent_outbound, mark delivered
            await conn.execute(
                """
                UPDATE reminder_outbox
                   SET state = 'delivered',
                       signal_timestamp = %s,
                       delivered_at = now(),
                       locked_until = NULL,
                       worker_id = NULL,
                       attempt = %s
                 WHERE id = %s
                """,
                (signal_ts, attempt, str(rid)),
            )
            # Record in recent_outbound for graph turn awareness.
            # title and reminder_type are carried from the outbox row so graph nodes
            # can classify terse replies (e.g. "I did it") without re-asking the user.
            # expires_at uses 24h for reminders to minimise stale-context misclassification.
            if signal_ts:
                reminder_title = row.get("body", "")[:200]  # truncate body to 200 chars for title proxy
                await conn.execute(
                    """
                    INSERT INTO recent_outbound
                      (peer, signal_timestamp, notion_page_id,
                       reminder_type, title, prompt_kind,
                       sent_at, awaiting_reply, expires_at)
                    VALUES (%s, %s, %s, 'reminder', %s, 'sent',
                            now(), true, now() + interval '24 hours')
                    ON CONFLICT DO NOTHING
                    """,
                    (peer, signal_ts, notion_page_id, reminder_title),
                )
            await conn.commit()

            if kind == "reminder":
                # Mark Notion reminder as sent (idempotent, outside transaction).
                try:
                    from app.tools.notion import complete_reminder
                    await complete_reminder(notion_page_id, "sent")
                except Exception as notion_err:
                    log.warning(
                        "reminder_worker.notion_complete_failed",
                        reminder_id=str(rid),
                        error=str(notion_err),
                    )

            log.info("reminder_worker.delivered", reminder_id=str(rid))
            delivered_count += 1

        except Exception as exc:
            err_str = str(exc)[:500]
            log.warning(
                "reminder_worker.delivery_failed",
                reminder_id=str(rid),
                attempt=attempt,
                error=err_str,
            )
            await conn.rollback()

            if attempt >= _MAX_ATTEMPTS:
                await conn.execute(
                    """
                    UPDATE reminder_outbox
                       SET state = 'dead',
                           last_error = %s,
                           locked_until = NULL,
                           worker_id = NULL,
                           attempt = %s
                     WHERE id = %s
                    """,
                    (err_str, attempt, str(rid)),
                )
                await conn.commit()
                await _throttled_ops_alert(
                    conn,
                    "reminder_dead",
                    f"Reminder {rid} exhausted {_MAX_ATTEMPTS} attempts: {err_str}",
                )
                await conn.commit()
            else:
                next_due = _next_due_at(attempt)
                await conn.execute(
                    """
                    UPDATE reminder_outbox
                       SET state = 'scheduled',
                           last_error = %s,
                           due_at = %s,
                           attempt = %s,
                           locked_until = NULL,
                           worker_id = NULL
                     WHERE id = %s
                    """,
                    (err_str, next_due, attempt, str(rid)),
                )
                await conn.commit()
            failed_count += 1

    log.info(
        "reminder_worker.cycle_done",
        claimed_count=len(rows),
        delivered_count=delivered_count,
        failed_count=failed_count,
    )


async def run_worker_once(
    *,
    database_url: str | None = None,
    signal_send_fn: Any = None,
) -> None:
    """Convenience wrapper: connect, dispatch, close.

    Called by the APScheduler reminder_dispatcher job.
    """
    from app.tools.db import get_connection_string

    conn_str = database_url or get_connection_string()
    async with await psycopg.AsyncConnection.connect(conn_str) as conn:
        await dispatch_due_reminders(conn, signal_send_fn=signal_send_fn)
