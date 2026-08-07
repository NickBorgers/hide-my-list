"""COMPLETE node: task completion + reward integration.

Marks the finished task as completed in Notion, triggers the reward subsystem,
and drafts a celebration message into pending_outbound.

Three sources can identify which task the user means, in order of authority:
a task named in the message itself, the unresolved reminder the assistant last
sent, and the task handed to the user by selection. The message wins because it
is the only source the user can steer — the other two are inferences from
context that goes stale, and when they both do, nothing else can resolve
"finished the dishes" to a page.

Reward integration implemented in PR-B5.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog

from app.graph.nodes._task_match import (
    DedupCandidate,
    dice_coefficient,
    normalize_title_tokens,
    open_non_reminder_tasks,
    parse_match_response,
    shortlist_duplicate_candidates,
)
from app.graph.state import ActiveTask, OutboundDraft, State

log = structlog.get_logger(__name__)

_ACTIVE_TASK_TTL = timedelta(hours=24)

# Stricter than intake's 0.85. The two failure modes differ in detectability,
# not just direction: intake's false match names the task it matched, so the
# user sees it that turn. A false match here stamps Completed At on a task the
# user has not done and celebrates the one they have — nothing surfaces the
# error until the task fails to reappear days later.
_TITLE_MATCH_CONFIDENCE_THRESHOLD = 0.90

# Below the shortlist default (0.4) because a message names a task in fewer
# words than the title carries: {laundry} against "Fold the laundry before bed"
# scores exactly 0.4 and would sit on the boundary. The score is a recall
# device for building the model's candidate set — it never authorizes a write.
_TITLE_MATCH_MIN_SCORE = 0.30

# Overlap at which a named task is taken to be the active task the user was
# already working on, short-circuiting the Notion and model round-trips.
_ACTIVE_TASK_AGREEMENT_SCORE = 0.40

# Subtracted from the user's message only, never from a task title. These words
# say "I finished something" without saying which something; leaving them in
# lets a message that names no task shortlist a task anyway. Kept deliberately
# small: a word wrongly included here re-opens the bug this path exists to fix,
# while a word wrongly omitted costs one Notion read that shortlists nothing.
# Words like "call", "clean", "pay", and "sort" are excluded for that reason —
# they are completion-flavored but they are also real task titles.
_COMPLETION_WORDS: frozenset[str] = frozenset({
    "done", "did", "doing", "finish", "finished", "finishing",
    "complete", "completed", "completing",
    "yep", "yeah", "yup", "yes", "ok", "okay", "sure",
    "that", "thats", "this", "these", "those", "one", "ones", "them", "they",
    "task", "tasks", "thing", "things", "item", "items", "list",
    "just", "now", "all", "already", "finally", "got", "have", "ive", "im",
    "out", "up", "off",
})


@dataclass(frozen=True)
class _CompletionTarget:
    source: Literal["active_task", "recent_outbound", "title_match"]
    page_id: str
    task_title: str
    work_type: str
    energy_required: str
    context_at: datetime | None
    signal_timestamp: int | None = None

    @property
    def needs_notion_write(self) -> bool:
        """Whether completing this target still has to write Status to Notion.

        Derived from the source rather than stored: reminder pages arrive here
        already Completed, because the worker completes them at delivery. A
        settable field would let a caller construct a recent_outbound target
        that writes anyway, and the write is the destructive half of this node.
        """
        return self.source != "recent_outbound"


@dataclass(frozen=True)
class _TitleMatch:
    """Outcome of resolving the user's message against open Notion tasks."""

    target: _CompletionTarget | None
    candidate_count: int
    confidence: float | None


def _parse_checkpoint_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _target_from_active_task(
    active_task: ActiveTask | None,
    *,
    now: datetime,
) -> _CompletionTarget | None:
    if not active_task:
        return None

    page_id = active_task.get("page_id", "")
    if not page_id:
        return None

    selected_at_value = active_task.get("selected_at")
    selected_at = _parse_checkpoint_datetime(selected_at_value)
    if selected_at is None:
        log.info(
            "complete_node.active_task_missing_selected_at",
            page_id=page_id,
            has_selected_at=bool(selected_at_value),
        )
        return None
    if now - selected_at > _ACTIVE_TASK_TTL:
        log.info(
            "complete_node.active_task_stale",
            page_id=page_id,
            selected_at=selected_at.isoformat(),
        )
        return None

    return _CompletionTarget(
        source="active_task",
        page_id=page_id,
        # `.get(key, default)` only fires on a missing key, so a stored empty
        # title used to reach the reward path verbatim. task_title is private
        # data written to the manifest — pass the empty string through rather
        # than fabricating a placeholder that would be stored as if it were the
        # user's own words.
        task_title=(active_task.get("title") or "").strip(),
        work_type=active_task.get("work_type", ""),
        energy_required=active_task.get("energy_required", ""),
        context_at=selected_at,
    )


async def _load_recent_outbound_target(peer: str) -> _CompletionTarget | None:
    if not peer or not os.environ.get("DATABASE_URL"):
        return None

    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT signal_timestamp, notion_page_id, title, sent_at
                  FROM recent_outbound
                 WHERE peer = %s
                   AND awaiting_reply = true
                   AND expires_at > now()
                 ORDER BY sent_at DESC, signal_timestamp DESC
                 LIMIT 1
                """,
                (peer,),
            )
            row = await cur.fetchone()

    if not row:
        return None

    signal_timestamp = int(row["signal_timestamp"])
    sent_at = row["sent_at"]
    if not isinstance(sent_at, datetime):
        sent_at = _parse_checkpoint_datetime(sent_at)
    elif sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)
    else:
        sent_at = sent_at.astimezone(UTC)

    return _CompletionTarget(
        source="recent_outbound",
        page_id=str(row["notion_page_id"]),
        task_title=str(row.get("title") or "").strip(),
        work_type="",
        energy_required="",
        context_at=sent_at,
        signal_timestamp=signal_timestamp,
    )


def _task_reference_tokens(incoming: str) -> set[str]:
    """Return the tokens in `incoming` that could name a task.

    Empty means the message reports a completion without saying which task —
    "done!", "I did it" — and the caller resolves from context instead. A
    non-empty result only earns a lookup, never a write.
    """
    return normalize_title_tokens(incoming) - _COMPLETION_WORDS


def _agrees_with_active_task(residue: set[str], active: _CompletionTarget | None) -> bool:
    """True when the named task is the one the user is already working on."""
    if active is None or not residue:
        return False
    return (
        dice_coefficient(residue, normalize_title_tokens(active.task_title))
        >= _ACTIVE_TASK_AGREEMENT_SCORE
    )


def _build_completion_match_prompt(incoming: str, candidates: list[DedupCandidate]) -> str:
    candidate_payload = [
        {"id": candidate.page_id, "title": candidate.title}
        for candidate in candidates
    ]
    return (
        "The user sent a message reporting that they finished something. Decide "
        "which candidate task, if any, the message says is ALREADY FINISHED.\n\n"
        "Match only when the message asserts that candidate is done. A message "
        "that mentions a task the user still intends to do, is asking about, or "
        "is about to start is NOT a match — return no match for those even when "
        "the wording overlaps a candidate title. The cost of a false match is "
        "high: it marks a task the user has not finished as completed. If "
        "uncertain, return no match.\n\n"
        f"User message: {incoming!r}\n"
        f"Candidates: {json.dumps(candidate_payload, ensure_ascii=True)}\n\n"
        "Return JSON only in this shape:\n"
        '{"matched_page_id": "<candidate id or null>", "confidence": 0.0}'
    )


async def _resolve_title_match(incoming: str, residue: set[str], *, now: datetime) -> _TitleMatch:
    """Resolve a task named in the message against open Notion tasks.

    Fail-soft by design: every failure returns no target so the caller falls
    through to context-based resolution. This must not raise — the node's outer
    handler emits complete_node.error, which the eval runner treats as the
    hand-written fallback path rather than real behavior.
    """
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.models import llm
        from app.tools import notion

        raw = await notion.query_all()
        # Reminder pages are filtered out here, so a title match can never
        # land on one — the needs_notion_write exemption stays unreachable
        # from this source.
        open_tasks = open_non_reminder_tasks(raw)

        # Every candidate goes to the model, even one that quotes a title
        # verbatim. Containing a task's words is not the same as saying it is
        # finished: "done, now I need to call mom" contains all of "Call mom"
        # while asserting the opposite. Only the whole sentence separates them,
        # so there is no lexical shortcut past this call.
        candidates = shortlist_duplicate_candidates(
            incoming,
            open_tasks,
            min_score=_TITLE_MATCH_MIN_SCORE,
            query_stopwords=_COMPLETION_WORDS,
        )
        if not candidates:
            return _TitleMatch(target=None, candidate_count=0, confidence=None)

        model = llm("cheap", caller="complete_title_match")
        response = await model.ainvoke([
            SystemMessage(content=_build_completion_match_prompt(incoming, candidates)),
            HumanMessage(content="Return only the JSON object."),
        ])
        parsed = parse_match_response(str(response.content), candidates)
        if parsed is None:
            return _TitleMatch(target=None, candidate_count=len(candidates), confidence=None)

        page_id, confidence = parsed
        if confidence < _TITLE_MATCH_CONFIDENCE_THRESHOLD:
            return _TitleMatch(target=None, candidate_count=len(candidates), confidence=confidence)

        matches = [candidate for candidate in candidates if candidate.page_id == page_id]
        if len(matches) != 1:
            return _TitleMatch(target=None, candidate_count=len(candidates), confidence=confidence)

        return _TitleMatch(
            target=_title_target(matches[0].page_id, matches[0].title, now=now),
            candidate_count=len(candidates),
            confidence=confidence,
        )
    except Exception:
        # Counts only — the message and titles are the user's private words.
        log.warning(
            "complete_node.title_match_failed",
            residue_token_count=len(residue),
            exc_info=True,
        )
        return _TitleMatch(target=None, candidate_count=0, confidence=None)


def _title_target(page_id: str, title: str, *, now: datetime) -> _CompletionTarget:
    return _CompletionTarget(
        source="title_match",
        page_id=page_id,
        task_title=title,
        work_type="",
        energy_required="",
        context_at=now,
    )


async def _clear_recent_outbound(peer: str, signal_timestamp: int) -> None:
    if not peer or not os.environ.get("DATABASE_URL"):
        return

    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        await conn.execute(
            """
            UPDATE recent_outbound
               SET awaiting_reply = false
             WHERE peer = %s
               AND signal_timestamp = %s
            """,
            (peer, signal_timestamp),
        )
        await conn.commit()


def _choose_completion_target(
    *,
    active_target: _CompletionTarget | None,
    recent_target: _CompletionTarget | None,
    title_target: _CompletionTarget | None = None,
) -> _CompletionTarget | None:
    if title_target:
        # Same page from two sources: keep the active task, which is the only
        # one carrying work_type and energy_required for the reward call.
        if active_target and active_target.page_id == title_target.page_id:
            return active_target
        # The user named a task. That outranks both inferences — including an
        # active task pointing at a different page, which would otherwise mark
        # the wrong one done.
        return title_target
    if recent_target and active_target:
        if active_target.context_at is None:
            return recent_target
        if recent_target.context_at is None:
            return active_target
        if recent_target.context_at >= active_target.context_at:
            return recent_target
        return active_target
    return recent_target or active_target


def _clarify_completion_target(peer: str) -> dict[str, Any]:
    no_task_draft: OutboundDraft = {
        "recipient": peer,
        "body": "I can mark that done. Which task did you mean?",
        "notion_page_id": None,
    }
    return {
        "pending_outbound": [no_task_draft],
        "conversation_state": "idle",
        "active_task": None,
    }


async def complete_node(state: State) -> dict[str, Any]:
    """COMPLETE handler: update Notion, call rewards.maybe_reward(), draft reply."""
    peer = state.get("peer", "")

    try:
        from app.tools import notion
        from app.tools.rewards import maybe_reward

        active_task = state.get("active_task")
        now = datetime.now(UTC)
        active_target = _target_from_active_task(active_task, now=now)
        try:
            recent_target = await _load_recent_outbound_target(peer)
        except Exception:
            log.warning(
                "complete_node.recent_outbound_load_failed",
                active_page_id=active_target.page_id if active_target else None,
                exc_info=True,
            )
            if not active_target:
                return _clarify_completion_target(peer)
            recent_target = None

        # Only look the message up when it names something. "done!" resolves
        # from context alone and must not pay for a Notion read or a model call.
        residue = _task_reference_tokens(state.get("incoming") or "")
        if residue and not _agrees_with_active_task(residue, active_target):
            title_match = await _resolve_title_match(
                state.get("incoming") or "", residue, now=now
            )
        else:
            title_match = _TitleMatch(target=None, candidate_count=0, confidence=None)

        target = _choose_completion_target(
            active_target=active_target,
            recent_target=recent_target,
            title_target=title_match.target,
        )

        # Ids and counts only — the residue tokens and task titles are the
        # user's own words and stay out of the logs.
        log.info(
            "complete_node.resolved_target",
            source=target.source if target else None,
            page_id=target.page_id if target else None,
            active_page_id=active_target.page_id if active_target else None,
            recent_page_id=recent_target.page_id if recent_target else None,
            title_page_id=title_match.target.page_id if title_match.target else None,
            candidate_count=title_match.candidate_count,
            match_confidence=title_match.confidence,
            residue_token_count=len(residue),
        )

        if not target:
            return _clarify_completion_target(peer)

        page_id = target.page_id
        task_title = target.task_title

        if target.needs_notion_write:
            await notion.update_status(page_id, "Completed")

        streak = state.get("streak", 0) + 1
        tasks_today = state.get("tasks_completed_today", 0) + 1

        reward_result = await maybe_reward(
            peer=peer,
            task_title=task_title,
            notion_page_id=page_id,
            streak=streak,
            work_type=target.work_type,
            energy_required=target.energy_required,
        )

        if target.source == "recent_outbound" and target.signal_timestamp is not None:
            try:
                await _clear_recent_outbound(peer, target.signal_timestamp)
            except Exception:
                log.warning(
                    "complete_node.recent_outbound_clear_failed",
                    page_id=page_id,
                    signal_timestamp=target.signal_timestamp,
                    exc_info=True,
                )

        reward_draft: OutboundDraft = {
            "recipient": peer,
            "body": reward_result["text"],
            "notion_page_id": page_id,
        }
        # Attach image if one was generated.
        # attachment_path is private; never log the path value.
        if reward_result["attachment_path"]:
            reward_draft["attachment_path"] = reward_result["attachment_path"]

        log.info(
            "complete_node.done",
            page_id=page_id,
            source=target.source,
            streak=streak,
        )
        return {
            "pending_outbound": [reward_draft],
            "active_task": None,
            "streak": streak,
            "tasks_completed_today": tasks_today,
            "conversation_state": "idle",
        }

    except Exception:
        log.exception("complete_node.error")
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "Got it, marked done! Nice work.",
            "notion_page_id": None,
        }
        return {
            "pending_outbound": [fallback],
            "active_task": None,
            "conversation_state": "idle",
        }
