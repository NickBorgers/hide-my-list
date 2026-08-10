"""Nightly backstop for deadline-driven reminder series."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.scheduler.reminder_scheduling import (
    ScheduledMilestone,
    cancel_outbox_rows,
    get_active_deadline_for_page,
    get_peer_for_task,
    schedule_for_task,
    supersede_ledger_rows,
)

log = structlog.get_logger(__name__)

_DEADLINE_MATCH_TOLERANCE_SECONDS = 60


async def run_reminder_scheduler(*, user_tz: str) -> None:
    """Schedule missing deadline series and reschedule edited deadlines."""
    from app.tools import notion, ops_alerts

    try:
        unscheduled = await notion.query_tasks_with_unscheduled_deadlines()
        scheduled = await notion.query_scheduled_tasks_with_deadlines()
    except Exception:
        log.exception("reminder_scheduler.notion_query_failed")
        await ops_alerts.enqueue(
            kind="reminder_scheduler_notion_failed",
            body="Deadline reminder scheduler could not query Notion.",
            severity="warning",
        )
        return

    for page in unscheduled.get("results", []):
        try:
            await _schedule_page(page, user_tz=user_tz, mark_scheduled=True)
        except Exception:
            log.exception("reminder_scheduler.orphan_failed")
            await ops_alerts.enqueue(
                kind="reminder_scheduler_task_failed",
                body="Deadline reminder scheduler failed for one unscheduled task.",
                severity="warning",
            )

    for page in scheduled.get("results", []):
        try:
            await _refresh_page_if_deadline_changed(page, user_tz=user_tz)
        except Exception:
            log.exception("reminder_scheduler.edit_check_failed")
            await ops_alerts.enqueue(
                kind="reminder_scheduler_task_failed",
                body="Deadline reminder scheduler failed while checking one scheduled task.",
                severity="warning",
            )


async def _schedule_page(page: dict[str, Any], *, user_tz: str, mark_scheduled: bool) -> None:
    from app.tools import notion
    from app.tools.db import get_db_conn

    parsed = _parse_page(page)
    if parsed is None:
        log.warning("reminder_scheduler.page_skipped", has_due_at=False)
        return
    page_id, deadline_at, urgency = parsed

    async with get_db_conn() as conn:
        peer = await get_peer_for_task(conn, page_id)
        if peer is None:
            await _alert_missing_peer(page_id)
            return
        existing_deadline = await get_active_deadline_for_page(conn, page_id)
        already_scheduled = (
            existing_deadline is not None and _deadlines_match(existing_deadline, deadline_at)
        )
        if already_scheduled:
            scheduled: list[ScheduledMilestone] = []
            failures: list[str] = []
        else:
            scheduled, failures = await schedule_for_task(
                conn,
                notion_page_id=page_id,
                peer=peer,
                deadline_at=deadline_at,
                urgency=urgency,
                now=datetime.now(UTC),
                user_tz=user_tz,
            )
    if mark_scheduled and (scheduled or already_scheduled) and not failures:
        await notion.mark_reminder_scheduled(page_id)

    log.info(
        "reminder_scheduler.page_scheduled",
        has_due_at=True,
        urgency=urgency,
        already_scheduled=already_scheduled,
        scheduled_count=len(scheduled),
        failure_count=len(failures),
    )


async def _refresh_page_if_deadline_changed(page: dict[str, Any], *, user_tz: str) -> None:
    parsed = _parse_page(page)
    if parsed is None:
        return
    page_id, current_deadline, urgency = parsed

    from app.tools import notion
    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        peer = await get_peer_for_task(conn, page_id)
        if peer is None:
            await _alert_missing_peer(page_id)
            return
        ledger_deadline = await get_active_deadline_for_page(conn, page_id)
        if ledger_deadline is not None and _deadlines_match(ledger_deadline, current_deadline):
            return
        outbox_ids = await supersede_ledger_rows(conn, page_id)
        await cancel_outbox_rows(conn, outbox_ids)
        await conn.commit()
        scheduled, failures = await schedule_for_task(
            conn,
            notion_page_id=page_id,
            peer=peer,
            deadline_at=current_deadline,
            urgency=urgency,
            now=datetime.now(UTC),
            user_tz=user_tz,
        )
        await conn.commit()

    if scheduled and not failures:
        await notion.mark_reminder_scheduled(page_id)
    log.info(
        "reminder_scheduler.deadline_refreshed",
        has_due_at=True,
        urgency=urgency,
        superseded_count=len(outbox_ids),
        scheduled_count=len(scheduled),
        failure_count=len(failures),
    )


def _deadlines_match(a: datetime, b: datetime) -> bool:
    return abs((a.astimezone(UTC) - b.astimezone(UTC)).total_seconds()) <= (
        _DEADLINE_MATCH_TOLERANCE_SECONDS
    )


async def _alert_missing_peer(page_id: str) -> None:
    from app.tools import ops_alerts

    log.warning("reminder_scheduler.missing_peer", page_id=page_id)
    await ops_alerts.enqueue(
        kind="reminder_scheduler_missing_peer",
        body="Deadline reminder scheduler found a task without recorded peer metadata.",
        severity="warning",
    )


def _parse_page(page: dict[str, Any]) -> tuple[str, datetime, int] | None:
    page_id = str(page.get("id", ""))
    props = page.get("properties", {})
    if not page_id or not isinstance(props, dict):
        return None

    deadline = _extract_date(props.get("Due At"))
    if deadline is None:
        return None
    urgency = _extract_number(props.get("Urgency")) or 50
    return page_id, deadline, urgency


def _extract_date(prop: Any) -> datetime | None:
    if not isinstance(prop, dict):
        return None
    date_value = prop.get("date")
    if not isinstance(date_value, dict):
        return None
    start = date_value.get("start")
    if not isinstance(start, str) or not start:
        return None
    parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _extract_number(prop: Any) -> int | None:
    if not isinstance(prop, dict):
        return None
    value = prop.get("number")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None
