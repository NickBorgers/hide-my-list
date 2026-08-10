"""Shared DB helper for deadline-driven reminder series."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import psycopg.rows
import structlog

from app.scheduler.deadline_planner import (
    Milestone,
    assign_slot,
    config_from_env,
    plan_milestones,
)

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ScheduledMilestone:
    label: str
    assigned_at: datetime
    outbox_id: uuid.UUID


async def schedule_for_task(
    conn: psycopg.AsyncConnection[Any],
    *,
    notion_page_id: str,
    peer: str,
    deadline_at: datetime,
    urgency: int,
    now: datetime,
    user_tz: str,
) -> tuple[list[ScheduledMilestone], list[str]]:
    """Plan, enqueue, and ledger a deadline reminder series for one task."""
    milestones = plan_milestones(deadline_at, urgency=urgency, now=now, user_tz=user_tz)
    if not milestones:
        return [], []

    cfg = config_from_env()
    existing = await _nearby_existing_slots(conn, milestones, cfg.max_drift_minutes)
    scheduled: list[ScheduledMilestone] = []
    failures: list[str] = []

    for milestone in milestones:
        assigned_at, fallback = assign_slot(
            milestone.ideal_at,
            existing,
            deadline_at,
            now=now,
            user_tz=user_tz,
            config=cfg,
        )
        if fallback:
            log.warning(
                "deadline_scheduling.slot_fallback",
                has_deadline=True,
                urgency=urgency,
                milestone_label=milestone.label,
            )
        existing.append(assigned_at)
        try:
            outbox_id = await _enqueue_deadline_row(
                conn,
                notion_page_id=notion_page_id,
                peer=peer,
                due_at=assigned_at,
                deadline_at=deadline_at,
                milestone_label=milestone.label,
            )
            await _insert_ledger_row(
                conn,
                notion_page_id=notion_page_id,
                deadline_at=deadline_at,
                urgency=urgency,
                milestone=milestone,
                assigned_at=assigned_at,
                outbox_id=outbox_id,
            )
            await conn.commit()
            scheduled.append(
                ScheduledMilestone(
                    label=milestone.label,
                    assigned_at=assigned_at,
                    outbox_id=outbox_id,
                )
            )
        except Exception:
            await conn.rollback()
            failures.append(milestone.label)
            log.exception(
                "deadline_scheduling.enqueue_failed",
                has_deadline=True,
                urgency=urgency,
                milestone_label=milestone.label,
            )

    return scheduled, failures


async def record_deadline_task_peer(
    conn: psycopg.AsyncConnection[Any],
    *,
    notion_page_id: str,
    peer: str,
) -> None:
    """Persist the Signal peer for a Notion task page with a deadline."""
    await conn.execute(
        """
        INSERT INTO deadline_task_peers (notion_page_id, peer)
        VALUES (%s, %s)
        ON CONFLICT (notion_page_id)
        DO UPDATE SET peer = EXCLUDED.peer,
                      recorded_at = now()
        """,
        (notion_page_id, peer),
    )
    await conn.commit()


async def get_peer_for_task(
    conn: psycopg.AsyncConnection[Any],
    notion_page_id: str,
) -> str | None:
    """Return the recorded Signal peer for a Notion task page."""
    async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            """
            SELECT peer
              FROM deadline_task_peers
             WHERE notion_page_id = %s
            """,
            (notion_page_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    peer = row["peer"]
    return peer if isinstance(peer, str) and peer else None


async def get_active_deadline_for_page(
    conn: psycopg.AsyncConnection[Any],
    notion_page_id: str,
) -> datetime | None:
    """Return the active ledger deadline for a task page, if one exists."""
    async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            """
            SELECT deadline_at
              FROM reminder_scheduling_ledger
             WHERE notion_page_id = %s
               AND superseded_at IS NULL
             ORDER BY scheduled_at DESC
             LIMIT 1
            """,
            (notion_page_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    deadline = row["deadline_at"]
    if isinstance(deadline, datetime):
        return deadline.astimezone(UTC)
    return None


async def supersede_ledger_rows(
    conn: psycopg.AsyncConnection[Any],
    notion_page_id: str,
) -> list[uuid.UUID]:
    """Mark active ledger rows superseded and return their outbox IDs."""
    async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            """
            UPDATE reminder_scheduling_ledger
               SET superseded_at = now()
             WHERE notion_page_id = %s
               AND superseded_at IS NULL
            RETURNING reminder_outbox_id
            """,
            (notion_page_id,),
        )
        rows = await cur.fetchall()
    return [uuid.UUID(str(row["reminder_outbox_id"])) for row in rows]


async def cancel_outbox_rows(
    conn: psycopg.AsyncConnection[Any],
    outbox_ids: list[uuid.UUID],
) -> None:
    """Mark pending/scheduled superseded deadline outbox rows dead."""
    if not outbox_ids:
        return
    await conn.execute(
        """
        UPDATE reminder_outbox
           SET state = 'dead',
               last_error = 'deadline superseded',
               locked_until = NULL,
               worker_id = NULL
         WHERE id = ANY(%s)
           AND kind = 'deadline'
           AND state IN ('pending', 'scheduled', 'delivering')
        """,
        ([str(item) for item in outbox_ids],),
    )


async def _nearby_existing_slots(
    conn: psycopg.AsyncConnection[Any],
    milestones: list[Milestone],
    max_drift_minutes: int,
) -> list[datetime]:
    if not milestones:
        return []
    starts = [m.ideal_at for m in milestones]
    lower = min(starts) - timedelta(minutes=max_drift_minutes)
    upper = max(starts) + timedelta(minutes=max_drift_minutes)
    async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            """
            SELECT assigned_slot_at
              FROM reminder_scheduling_ledger
             WHERE superseded_at IS NULL
               AND assigned_slot_at BETWEEN %s AND %s
            """,
            (lower, upper),
        )
        rows = await cur.fetchall()
    return [
        row["assigned_slot_at"].astimezone(UTC)
        for row in rows
        if isinstance(row.get("assigned_slot_at"), datetime)
    ]


async def _enqueue_deadline_row(
    conn: psycopg.AsyncConnection[Any],
    *,
    notion_page_id: str,
    peer: str,
    due_at: datetime,
    deadline_at: datetime,
    milestone_label: str,
) -> uuid.UUID:
    outbox_id = uuid.uuid4()
    idempotency_key = (
        f"deadline-{notion_page_id}-{milestone_label}-"
        f"{deadline_at.astimezone(UTC).isoformat()}"
    )
    await conn.execute(
        """
        INSERT INTO reminder_outbox
          (id, notion_page_id, peer, body, due_at, state, idempotency_key, kind)
        VALUES (%s, %s, %s, %s, %s, 'pending', %s, 'deadline')
        """,
        (
            str(outbox_id),
            notion_page_id,
            peer,
            _deadline_body(milestone_label),
            due_at,
            idempotency_key,
        ),
    )
    return outbox_id


async def _insert_ledger_row(
    conn: psycopg.AsyncConnection[Any],
    *,
    notion_page_id: str,
    deadline_at: datetime,
    urgency: int,
    milestone: Milestone,
    assigned_at: datetime,
    outbox_id: uuid.UUID,
) -> None:
    await conn.execute(
        """
        INSERT INTO reminder_scheduling_ledger
          (notion_page_id, deadline_at, urgency, tier, milestone_label,
           ideal_slot_at, assigned_slot_at, reminder_outbox_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            notion_page_id,
            deadline_at,
            urgency,
            milestone.tier,
            milestone.label,
            milestone.ideal_at,
            assigned_at,
            str(outbox_id),
        ),
    )


def _deadline_body(milestone_label: str) -> str:
    return "Deadline nudge for this task. Want one tiny next step?"
