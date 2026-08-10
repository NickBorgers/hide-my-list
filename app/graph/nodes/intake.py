"""ADD_TASK node: task intake with label inference, sub-task generation, reminder detection.

Ports docs/ai-prompts/intake.md (464 lines) behavior:
- Aggressive label inference (urgency, work_type, time_estimate)
- Sub-task generation for every task
- Reminder detection (wall-clock time → outbox row)
- Clarification flow (max 3 questions)
- Reschedule from recent_outbound context

When a reminder is detected:
- Creates Notion row via app/tools/notion.create_reminder()
- Enqueues outbox row via app/tools/reminders.enqueue()
- Uses app/tools/time_context to resolve user-local time

REMINDER PERSISTENCE uses the outbox state machine; the reminder worker (APScheduler) handles delivery.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import structlog

from app.graph.nodes._task_match import (
    DedupCandidate,
    open_non_reminder_tasks,
    parse_match_response,
    shortlist_duplicate_candidates,
)
from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)

# Intake's own threshold. A false match here silently folds the user's new task
# into an existing page, so the bar is high — but the reply names the matched
# task, making a wrong call visible and correctable in the same turn.
_DEDUP_CONFIDENCE_THRESHOLD = 0.85


@dataclass(frozen=True)
class DedupMatch:
    """Confirmed existing task match."""

    page_id: str
    title: str
    candidate_count: int


async def intake_node(state: State) -> dict[str, Any]:
    """ADD_TASK handler: infer labels, generate sub-tasks, detect reminders."""
    peer = state.get("peer", "")

    try:
        from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

        from app.models import llm
        from app.tools import notion
        from app.tools.time_context import get_time_context

        incoming = state.get("incoming", "")
        user_prefs = state.get("user_prefs", {})
        messages_history: list[AnyMessage] = state.get("messages", [])

        # Build conversation history summary
        history_lines = []
        for msg in messages_history[-6:]:
            role = getattr(msg, "type", "message")
            content = str(getattr(msg, "content", ""))
            history_lines.append(f"{role}: {content[:200]}")
        conversation_history = "\n".join(history_lines) or "No prior context."

        # Get time context in user's timezone
        time_ctx = get_time_context("now")
        current_time = time_ctx["reference_local"]
        user_timezone = time_ctx["user_timezone"]

        # User preferences context
        user_prefs_context = _build_prefs_context(user_prefs)

        # Clarification count tracking (use messages history length as proxy)
        clarification_count = 0

        from app.prompts.loader import render_with_defaults
        prompt_context = {
            "user_message": incoming,
            "conversation_history": conversation_history,
            "user_preferences_context": user_prefs_context,
            "clarification_count": clarification_count,
            "current_time": current_time,
            "user_timezone": user_timezone,
        }
        prompt_text = render_with_defaults("intake.md.j2", prompt_context)

        model = llm("medium", caller="intake")
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content=f"The user said: {incoming!r}"),
        ]

        response = await model.ainvoke(messages)
        response_text = str(response.content).strip()

        parsed = _parse_intake_response(response_text)

        if parsed is None:
            # Unparseable model output (e.g. truncated mid-JSON). Do not fake a
            # success: preserve capture, alert the operator, tell the truth.
            return await _handle_parse_failure(peer=peer, incoming=incoming)

        if parsed.get("action") == "clarify":
            question = parsed.get("clarification_question", "Which task are you thinking of?")
            clarify_draft: OutboundDraft = {
                "recipient": peer,
                "body": question,
                "notion_page_id": None,
            }
            return {"pending_outbound": [clarify_draft]}

        # Action is "save"
        # A blank title is as unusable as a missing one: it propagates into the
        # Notion page, active_task, and the reward manifest, leaving the user
        # with a nameless task. Fall back to their own words, and treat a save
        # we cannot name at all as a parse failure rather than writing a
        # nameless page.
        task_title = str(parsed.get("title") or "").strip() or incoming[:200].strip()
        if not task_title:
            log.warning("intake_node.blank_title")
            blank_title_draft: OutboundDraft = {
                "recipient": peer,
                "body": "What should I call that one?",
                "notion_page_id": None,
            }
            return {"pending_outbound": [blank_title_draft]}
        work_type = str(parsed.get("work_type", "focus"))
        urgency = int(parsed.get("urgency", 50))
        time_estimate = int(parsed.get("time_estimate_minutes", 30))
        energy_required = str(parsed.get("energy_required", "Medium"))
        is_reminder = bool(parsed.get("is_reminder", False))
        remind_at_str = parsed.get("remind_at")
        due_at_str = parsed.get("due_at")
        inline_steps = parsed.get("inline_steps", "")
        use_hidden_subtasks = bool(parsed.get("use_hidden_subtasks", False)) and not is_reminder
        sub_tasks = parsed.get("sub_tasks", [])
        confirmation_message = parsed.get("confirmation_message", f"Got it — {work_type}, ~{time_estimate} min.")

        log.info(
            "intake_node.parsed",
            is_reminder=is_reminder,
            has_remind_at=bool(remind_at_str),
            has_due_at=bool(due_at_str),
            urgency=urgency,
            time_estimate=time_estimate,
        )

        deadline_at: datetime | None = None
        if due_at_str:
            try:
                deadline_at = _parse_iso_strict(due_at_str)
            except ValueError:
                log.info("intake_node.due_at_parse_failed", has_due_at=True)

        duplicate_matched = False
        dedup_match: DedupMatch | None = None
        if is_reminder and remind_at_str:
            notion_page = await _create_reminder(
                peer=peer,
                title=task_title,
                work_type=work_type,
                energy_required=energy_required,
                remind_at_str=remind_at_str,
            )
        else:
            log.info("intake_node.no_reminder", has_remind_at=bool(remind_at_str), is_reminder=is_reminder)
            dedup_match = await _find_existing_task_match(task_title)
            if dedup_match is not None:
                if deadline_at is not None:
                    try:
                        await notion.update_property(
                            page_id=dedup_match.page_id,
                            prop_json={
                                "properties": {
                                    "Due At": {"date": {"start": deadline_at.isoformat()}}
                                }
                            },
                        )
                    except Exception:
                        log.exception(
                            "intake_node.duplicate_deadline_update_failed",
                            candidate_count=dedup_match.candidate_count,
                            matched=True,
                        )
                        dedup_match = None
                if dedup_match is not None:
                    duplicate_matched = True
                    notion_page = {"id": dedup_match.page_id}
                    if deadline_at is not None:
                        confirmation_message = "That one's already on your list — I've added the deadline to it."
                    else:
                        confirmation_message = f"That one's already on your list — {dedup_match.title}."
                    log.info(
                        "intake_node.duplicate_detected",
                        candidate_count=dedup_match.candidate_count,
                        matched=True,
                        has_due_at=deadline_at is not None,
                    )
                else:
                    notion_page = await notion.create_task(
                        title=task_title,
                        work_type=work_type,
                        urgency=urgency,
                        time_estimate=time_estimate,
                        energy_required=energy_required,
                        inline_steps=inline_steps,
                        due_at_iso=deadline_at.isoformat() if deadline_at else None,
                    )
            else:
                notion_page = await notion.create_task(
                    title=task_title,
                    work_type=work_type,
                    urgency=urgency,
                    time_estimate=time_estimate,
                    energy_required=energy_required,
                    inline_steps=inline_steps,
                    due_at_iso=deadline_at.isoformat() if deadline_at else None,
                )

        page_id = (notion_page or {}).get("id")
        assigned_slots: list[tuple[str, datetime]] = []
        if deadline_at is not None and page_id and not is_reminder:
            assigned_slots = await _schedule_deadline_series(
                page_id=page_id,
                peer=peer,
                deadline_at=deadline_at,
                urgency=urgency,
                user_timezone=user_timezone,
                refresh_existing_deadline=duplicate_matched,
            )

        # Create hidden sub-tasks if needed
        if use_hidden_subtasks and sub_tasks and page_id and not duplicate_matched:
            for sub in sub_tasks:
                try:
                    await notion.create_task(
                        title=sub.get("title", "Sub-task"),
                        work_type=work_type,
                        urgency=urgency,
                        time_estimate=sub.get("time_estimate_minutes", 15),
                        energy_required=energy_required,
                        parent_id=page_id,
                        sequence=sub.get("sequence", 1),
                    )
                except Exception:
                    log.exception("intake_node.subtask_create_failed", page_id=page_id)

        if assigned_slots:
            from app.scheduler.deadline_planner import format_reminder_summary

            summary = format_reminder_summary(assigned_slots, user_timezone)
            if summary:
                confirmation_message = f"{confirmation_message} {summary}"

        draft_task_title = dedup_match.title if duplicate_matched and dedup_match else task_title
        draft: OutboundDraft = {
            "recipient": peer,
            "body": confirmation_message,
            "notion_page_id": page_id,
        }
        if draft_task_title:
            draft["notion_page_title"] = draft_task_title

        if duplicate_matched:
            log.info(
                "intake_node.saved",
                is_reminder=is_reminder,
                duplicate_matched=True,
                has_due_at=deadline_at is not None,
                scheduled_count=len(assigned_slots),
            )
        else:
            log.info(
                "intake_node.saved",
                page_id=page_id,
                is_reminder=is_reminder,
                duplicate_matched=False,
                has_due_at=deadline_at is not None,
                scheduled_count=len(assigned_slots),
            )
        return {
            "pending_outbound": [draft],
            "conversation_state": "idle",
        }

    except Exception:
        log.exception("intake_node.error", has_peer=bool(peer))
        # Something raised before the task was stored. Do not claim success —
        # nothing was saved. Alert the operator and ask the user to resend.
        try:
            from app.tools import ops_alerts
            await ops_alerts.enqueue(
                kind="intake_node_error",
                body="Intake node raised before saving; the task was not stored. See logs.",
                severity="warning",
            )
        except Exception:
            log.exception("intake_node.error.ops_alert_failed", has_peer=bool(peer))
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "Something hiccupped on my end with that one — mind sending it again?",
            "notion_page_id": None,
        }
        return {"pending_outbound": [fallback]}


async def _find_existing_task_match(proposed_title: str) -> DedupMatch | None:
    """Return a high-confidence existing task match, or None.

    This guard is fail-open by design: every error returns None so intake still
    creates the user's task rather than dropping it.
    """
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.models import llm
        from app.tools import notion

        raw = await notion.query_all()
        existing = open_non_reminder_tasks(raw)
        candidates = shortlist_duplicate_candidates(proposed_title, existing)
        if not candidates:
            return None

        prompt = _build_dedup_prompt(proposed_title, candidates)
        model = llm("cheap", caller="intake_dedup")
        response = await model.ainvoke([
            SystemMessage(content=prompt),
            HumanMessage(content="Return only the JSON object."),
        ])
        parsed = parse_match_response(str(response.content), candidates)
        if parsed is None:
            return None
        page_id, confidence = parsed
        if confidence < _DEDUP_CONFIDENCE_THRESHOLD:
            return None
        matches = [candidate for candidate in candidates if candidate.page_id == page_id]
        if len(matches) != 1:
            return None
        match = matches[0]
        return DedupMatch(page_id=match.page_id, title=match.title, candidate_count=len(candidates))
    except Exception:
        log.exception("intake_node.dedup_failed")
        return None


def _build_dedup_prompt(proposed_title: str, candidates: list[DedupCandidate]) -> str:
    candidate_payload = [
        {"id": candidate.page_id, "title": candidate.title}
        for candidate in candidates
    ]
    return (
        "Decide whether the proposed task is the same real-world action as exactly one "
        "candidate task. Match only when the user would reasonably expect one list item, "
        "not merely because the tasks share a topic. The cost of a false match is high: "
        "if uncertain, return no match.\n\n"
        f"Proposed task: {proposed_title!r}\n"
        f"Candidates: {json.dumps(candidate_payload, ensure_ascii=True)}\n\n"
        "Return JSON only in this shape:\n"
        '{"matched_page_id": "<candidate id or null>", "confidence": 0.0}'
    )


async def _handle_parse_failure(*, peer: str, incoming: str) -> dict[str, Any]:
    """Handle an unparseable intake LLM response without faking success.

    Preserves capture by saving the user's raw message as a plain task — titled
    from their own words, never the garbled model output — alerts the operator
    so model degradation is visible, and returns an honest confirmation that
    flags the un-captured timing. Never claims a reminder was set.
    """
    from app.tools import notion

    log.error("intake_node.parse_failed")

    # Deliberately uncaught: if the save fails there is nothing on the list, and
    # the honest answer is the caller's error path ("mind sending it again?").
    # Swallowing it here would reply "Added that to your list" over an empty
    # Notion — the same fabricated success this function exists to prevent,
    # moved one level down.
    notion_page = await notion.create_task(
        title=incoming[:200],
        work_type="focus",
        urgency=50,
        time_estimate=30,
        energy_required="Medium",
    )
    page_id = (notion_page or {}).get("id")

    try:
        from app.tools import ops_alerts
        await ops_alerts.enqueue(
            kind="intake_parse_failed",
            body=(
                "Intake LLM returned unparseable output; saved a raw task with no "
                "labels or reminder. Model may be truncating or degraded."
            ),
            severity="warning",
        )
    except Exception:
        log.exception("intake_node.parse_failed.ops_alert_failed")

    draft: OutboundDraft = {
        "recipient": peer,
        "body": (
            "Added that to your list. I couldn't quite pin down the timing on that "
            "one though — if there's a deadline or a time you want a nudge, send it "
            "again and I'll set the reminder."
        ),
        "notion_page_id": page_id,
    }
    return {"pending_outbound": [draft], "conversation_state": "idle"}


async def _create_reminder(
    *,
    peer: str,
    title: str,
    work_type: str,
    energy_required: str,
    remind_at_str: str,
) -> dict[str, Any]:
    """Create a Notion reminder row and enqueue the outbox row.

    notion.create_reminder() accepts: title, remind_at_iso, work_type, energy_required.
    Urgency, time_estimate, and inline_steps are set by the Notion function internally.
    """
    from app.tools import notion
    from app.tools.db import get_db_conn
    from app.tools.reminders import enqueue

    # Parse remind_at to datetime
    remind_at = _parse_remind_at(remind_at_str)

    notion_page = await notion.create_reminder(
        title=title,
        remind_at_iso=remind_at.isoformat(),
        work_type=work_type,
        energy_required=energy_required,
    )

    page_id = (notion_page or {}).get("id", "")

    # Enqueue in the outbox for at-least-once delivery
    if page_id:
        idempotency_key = f"intake-{page_id}"
        try:
            async with get_db_conn() as conn:
                await enqueue(
                    conn,
                    notion_page_id=page_id,
                    peer=peer,
                    body=f"Hey — {title}",
                    due_at=remind_at,
                    idempotency_key=idempotency_key,
                )
            log.info(
                "intake_node.outbox_enqueued",
                page_id=page_id,
                due_at=remind_at.isoformat() if remind_at else None,
                idempotency_key=idempotency_key,
            )
        except Exception:
            log.exception("intake_node.enqueue_failed", page_id=page_id)
            # Notion row exists but outbox write failed — reminder won't be delivered
            # automatically. Emit ops alert so the operator can investigate.
            try:
                from app.tools import ops_alerts
                await ops_alerts.enqueue(
                    kind="reminder_enqueue_failed",
                    body=f"Reminder outbox enqueue failed for page {page_id!r}. Reminder exists in Notion but will not be delivered until the outbox row is created.",
                    severity="warning",
                )
            except Exception:
                log.exception("intake_node.ops_alert_failed", page_id=page_id)

    return notion_page or {}


def _parse_remind_at(remind_at_str: str) -> datetime:
    """Parse an ISO 8601 string to a UTC-aware datetime."""
    try:
        normalized = remind_at_str.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid ISO timestamp") from exc


def _parse_iso_strict(iso_str: str) -> datetime:
    """Parse an ISO 8601 string to a UTC-aware datetime."""
    if not isinstance(iso_str, str) or not iso_str.strip():
        raise ValueError("invalid ISO timestamp")
    try:
        normalized = iso_str.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid ISO timestamp") from exc


async def _schedule_deadline_series(
    *,
    page_id: str,
    peer: str,
    deadline_at: datetime,
    urgency: int,
    user_timezone: str,
    refresh_existing_deadline: bool = False,
) -> list[tuple[str, datetime]]:
    """Schedule a deadline milestone series after a Notion task is created."""
    try:
        from app.scheduler.reminder_scheduling import (
            cancel_outbox_rows,
            get_active_deadline_for_page,
            record_deadline_task_peer,
            schedule_for_task,
            supersede_ledger_rows,
        )
        from app.tools import notion
        from app.tools.db import get_db_conn

        async with get_db_conn() as conn:
            await record_deadline_task_peer(conn, notion_page_id=page_id, peer=peer)
            if refresh_existing_deadline:
                active_deadline = await get_active_deadline_for_page(conn, page_id)
                if active_deadline is not None and _deadlines_match(active_deadline, deadline_at):
                    return []
                outbox_ids = await supersede_ledger_rows(conn, page_id)
                await cancel_outbox_rows(conn, outbox_ids)
                await conn.commit()
            scheduled, failures = await schedule_for_task(
                conn,
                notion_page_id=page_id,
                peer=peer,
                deadline_at=deadline_at,
                urgency=urgency,
                now=datetime.now(UTC),
                user_tz=user_timezone,
            )
        if scheduled and not failures:
            try:
                await notion.mark_reminder_scheduled(page_id)
            except Exception:
                log.exception("intake_node.mark_scheduled_failed", page_id=page_id)
        log.info(
            "intake_node.deadline_series_scheduled",
            page_id=page_id,
            scheduled_count=len(scheduled),
            failure_count=len(failures),
        )
        return [(item.label, item.assigned_at) for item in scheduled]
    except Exception:
        log.exception("intake_node.deadline_series_failed", page_id=page_id)
        return []


def _deadlines_match(a: datetime, b: datetime) -> bool:
    return abs((a.astimezone(UTC) - b.astimezone(UTC)).total_seconds()) <= 1


def _parse_intake_response(response_text: str) -> dict[str, Any] | None:
    """Extract JSON from an LLM intake response.

    Returns None when no JSON object can be parsed — e.g. the response was
    truncated at the output-token ceiling, or the model returned prose. The
    caller MUST treat None as a parse failure, never as a non-reminder task: a
    fabricated default here would let a dropped reminder masquerade as a
    confirmed plain task (the exact bug this guards against).
    """
    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if json_match:
        try:
            loaded = json.loads(json_match.group())
            if isinstance(loaded, dict):
                return cast(dict[str, Any], loaded)
        except json.JSONDecodeError:
            pass

    return None


def _build_prefs_context(user_prefs: Mapping[str, object]) -> str:
    """Build user preferences context string for the prompt."""
    if not user_prefs:
        return "No user preferences configured."

    lines = ["This user has the following preferences:"]
    if tz := user_prefs.get("timezone"):
        lines.append(f"- Timezone: {tz}")
    wt = user_prefs.get("preferred_work_types")
    if isinstance(wt, list) and all(isinstance(item, str) for item in wt):
        lines.append(f"- Preferred work types: {', '.join(wt)}")
    if energy := user_prefs.get("default_energy"):
        lines.append(f"- Default energy level: {energy}")
    return "\n".join(lines)
