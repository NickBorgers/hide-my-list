"""COMPLETE node: task completion + reward integration.

Marks the finished task as completed in Notion, triggers the reward subsystem,
and drafts a celebration message into pending_outbound.

Three sources can identify which task the user means, in order of authority:
a task named in the message itself, the unresolved reminder the assistant last
sent, and the task handed to the user by selection. The message wins because it
is the only source the user can steer — the other two are inferences from
context that goes stale, and when they both do, nothing else can resolve
"finished the dishes" to a page.

When none of the three resolves, the node asks — and records the question in
`pending_clarification` so the reply that answers it comes back here instead of
re-entering cold and re-asking. A failure in any one source narrows the answer,
never the question: the lookups are independent, so a dead Postgres or an empty
shortlist must not stop the others from running.

Reward integration implemented in PR-B5.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog

from app.graph.nodes._task_match import (
    DedupCandidate,
    normalize_title_tokens,
    open_non_reminder_tasks,
    parse_match_response,
    shortlist_duplicate_candidates,
)
from app.graph.state import (
    ActiveTask,
    ClarificationCandidate,
    OutboundDraft,
    PendingClarification,
    State,
)

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

# When nothing clears _TITLE_MATCH_MIN_SCORE, the open list goes to the model
# anyway, ranked but unfiltered. Token overlap answers "do these two strings
# share words", and the question here is "does this message report this task as
# done" — the two come apart exactly where a user paraphrases their own task,
# which is the normal way to describe finishing something. A zero score is
# evidence that the words differ, not evidence that the task is unrelated, so
# it may not decide on its own that the model never sees the list.
#
# The cap bounds the prompt, not the recall: it only binds on lists longer than
# this, and complete_node.candidate_set_truncated says when it did.
_FALLBACK_CANDIDATE_LIMIT = 40

# Re-asks before the agent stops asking. The first question is open ("which
# task did you mean?"); the second names concrete options, per
# design/adhd-priorities.md — "if you must ask one question, offer 2-3
# constrained choices, not open-ended". After that the agent stops rather than
# spending more of the user's attention on a question that is not landing.
_MAX_CLARIFICATION_ATTEMPTS = 2

# Options named in a single re-ask. Recognition beats recall, but a long list
# is its own decision load.
_CLARIFICATION_OPTION_LIMIT = 3

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
    candidates: tuple[DedupCandidate, ...] = ()
    # True when the candidate set is the whole open list rather than titles the
    # message actually overlaps. It changes what a null match means: over scored
    # candidates the model is saying "the task you named is not done", which is
    # a reason to stop; over the whole list it is saying "I could not tell",
    # which is not.
    widened: bool = False


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


def _build_completion_match_prompt(
    incoming: str,
    candidates: list[DedupCandidate],
    *,
    answering_clarification: bool = False,
    offered: tuple[DedupCandidate, ...] = (),
) -> str:
    """Ask the model which candidate the message resolves to.

    Two framings, because the message means different things in the two cases.
    A standalone completion has to carry the claim itself, so the model is told
    to reject anything that does not assert a task is finished. An answer to
    "which task did you mean?" carries no such claim and never will: the user
    already said they finished something on the previous turn and was asked
    only *which*. Judging "the garden one" against the standalone rule rejects
    it every time — correctly, for a rule that does not apply to it.

    `offered` is the option list the previous turn actually named, in the order
    it named them. Offering "was it A, B, or C?" and then being unable to read
    "the second one" is worse than never offering choices: it invites the short
    answer and then demands the long one. Enumerating them here is what gives
    an ordinal a referent.
    """
    candidate_payload = [
        {"id": candidate.page_id, "title": candidate.title}
        for candidate in candidates
    ]
    if answering_clarification:
        instructions = (
            "The user reported finishing a task and was asked which one they "
            "meant. This message is their answer.\n\n"
            "Decide which candidate task the answer identifies. The answer does "
            "not need to say the task is done — that was already said on the "
            "previous turn. It only has to point at a task, by name, by "
            "paraphrase, or by some detail that distinguishes it.\n\n"
            "Match only when the answer points at exactly one candidate. If it "
            "identifies none of them, points at several equally well, or "
            "changes the subject, return no match. The cost of a false match is "
            "high: it marks a task the user has not finished as completed. If "
            "uncertain, return no match."
        )
        if offered:
            numbered = "\n".join(
                f"{index}. {candidate.title}"
                for index, candidate in enumerate(offered, start=1)
            )
            instructions += (
                "\n\nThese options were named to the user, in this order:\n"
                f"{numbered}\n"
                'An answer that picks by position — "the first one", "the '
                'second", "the last one" — refers to this numbering. Resolve it '
                "to that option's id."
            )
    else:
        instructions = (
            "The user sent a message reporting that they finished something. "
            "Decide which candidate task, if any, the message says is ALREADY "
            "FINISHED.\n\n"
            "Match only when the message asserts that candidate is done. A "
            "message that mentions a task the user still intends to do, is "
            "asking about, or is about to start is NOT a match — return no "
            "match for those even when the wording overlaps a candidate title. "
            "The cost of a false match is high: it marks a task the user has "
            "not finished as completed. If uncertain, return no match."
        )
    return (
        f"{instructions}\n\n"
        f"User message: {incoming!r}\n"
        f"Candidates: {json.dumps(candidate_payload, ensure_ascii=True)}\n\n"
        "Return JSON only in this shape:\n"
        '{"matched_page_id": "<candidate id or null>", "confidence": 0.0}'
    )


def _reoffer_candidates(
    offered: tuple[ClarificationCandidate, ...],
    open_tasks: list[Mapping[str, str]],
) -> list[DedupCandidate]:
    """Rebuild the previous turn's options, in the order they were offered.

    Filtered against the current open list and re-read from it, so an option
    that has since been completed or renamed cannot come back through a stale
    checkpoint. Order is the offered order because that is what an ordinal
    answer refers to; the score is unused here and carries no ranking claim.
    """
    open_titles = {task["id"]: task["title"] for task in open_tasks}
    rebuilt: list[DedupCandidate] = []
    seen: set[str] = set()
    for option in offered:
        if not isinstance(option, dict):
            continue
        page_id = option.get("page_id", "")
        if not page_id or page_id in seen or page_id not in open_titles:
            continue
        seen.add(page_id)
        rebuilt.append(DedupCandidate(page_id=page_id, title=open_titles[page_id], score=0.0))
    return rebuilt


async def _resolve_title_match(
    incoming: str,
    residue: set[str],
    *,
    now: datetime,
    answering_clarification: bool = False,
    offered: tuple[ClarificationCandidate, ...] = (),
) -> _TitleMatch:
    """Resolve a task named in the message against open Notion tasks.

    Fail-soft by design: every failure returns no target so the caller falls
    through to context-based resolution. This must not raise — the node's outer
    handler emits complete_node.error, which the eval runner treats as the
    hand-written fallback path rather than real behavior.

    Scope: the candidate set is every open non-reminder task, unfiltered by
    peer. That is deliberate and is the documented data model, not a missing
    authorization check — the Notion database holds one person's tasks and has
    no owner column, and AUTHORIZED_PEERS lists that person's own addresses.
    See "Scope and Ownership" in docs/notion-schema.md. The access boundary is
    the allowlist at the Signal ingress; past it there is nothing to partition.
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
        widened = False
        if not candidates:
            # The message named something the shortlist could not place. Rank
            # the whole open list and let the model read it rather than
            # answering "which task did you mean?" without having looked.
            widened = True
            candidates = shortlist_duplicate_candidates(
                incoming,
                open_tasks,
                limit=_FALLBACK_CANDIDATE_LIMIT,
                min_score=0.0,
                query_stopwords=_COMPLETION_WORDS,
            )
            if len(open_tasks) > _FALLBACK_CANDIDATE_LIMIT:
                log.info(
                    "complete_node.candidate_set_truncated",
                    open_task_count=len(open_tasks),
                    limit=_FALLBACK_CANDIDATE_LIMIT,
                )

        # The options the previous turn named lead the list, in that order, and
        # are never dropped by ranking — an ordinal answer has no other referent,
        # and a message like "the second one" scores nothing against any title.
        reoffered = _reoffer_candidates(offered, open_tasks) if answering_clarification else []
        if reoffered:
            reoffered_ids = {candidate.page_id for candidate in reoffered}
            candidates = reoffered + [
                candidate for candidate in candidates
                if candidate.page_id not in reoffered_ids
            ]

        if not candidates:
            return _TitleMatch(target=None, candidate_count=0, confidence=None)

        def outcome(
            target: _CompletionTarget | None, confidence: float | None
        ) -> _TitleMatch:
            """Attach the candidate set to every verdict, matched or not.

            The rejected candidates are what the next turn's question offers as
            options, so they have to survive a null match.
            """
            return _TitleMatch(
                target=target,
                candidate_count=len(candidates),
                confidence=confidence,
                candidates=tuple(candidates),
                widened=widened,
            )

        model = llm("cheap", caller="complete_title_match")
        response = await model.ainvoke([
            SystemMessage(content=_build_completion_match_prompt(
                incoming,
                candidates,
                answering_clarification=answering_clarification,
                offered=tuple(reoffered),
            )),
            HumanMessage(content="Return only the JSON object."),
        ])
        parsed = parse_match_response(str(response.content), candidates)
        if parsed is None:
            return outcome(None, None)

        page_id, confidence = parsed
        if confidence < _TITLE_MATCH_CONFIDENCE_THRESHOLD:
            return outcome(None, confidence)

        matches = [candidate for candidate in candidates if candidate.page_id == page_id]
        if len(matches) != 1:
            return outcome(None, confidence)

        return outcome(
            _title_target(matches[0].page_id, matches[0].title, now=now), confidence
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


async def _clear_recent_outbound(
    peer: str, signal_timestamp: int, notion_page_id: str = ""
) -> None:
    """Resolve the reminder rows this completion answers.

    Scoped by page, not by the single delivery the user replied to. A task can
    have several reminders in flight — migration 0007 dropped the UNIQUE on
    `reminder_outbox.notion_page_id` so deadline milestones could stack — and
    each delivery writes its own `recent_outbound` row. Clearing only the row
    matching `signal_timestamp` leaves the siblings live for their full 24h
    window, where a later unrelated "done" resolves one of them: the wrong task
    marked complete, and a reward celebrating work that was already finished
    and already celebrated. `docs/reward-system.md` ties rewards to actual
    completion, so that is a spec violation, not merely untidy state.

    `signal_timestamp` stays in the predicate as a fallback so a target that
    somehow carries no page id still resolves the row it came from.
    """
    if not peer or not os.environ.get("DATABASE_URL"):
        return

    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        await conn.execute(
            """
            UPDATE recent_outbound
               SET awaiting_reply = false
             WHERE peer = %s
               AND awaiting_reply = true
               AND (
                     signal_timestamp = %s
                     OR (%s <> '' AND notion_page_id = %s)
                   )
            """,
            (peer, signal_timestamp, notion_page_id, notion_page_id),
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


def _format_options(titles: list[str]) -> str:
    """Render task titles as a natural inline list."""
    if len(titles) == 1:
        return titles[0]
    if len(titles) == 2:
        return f"{titles[0]} or {titles[1]}"
    return f"{', '.join(titles[:-1])}, or {titles[-1]}"


def _clarification_body(attempts: int, candidates: tuple[DedupCandidate, ...]) -> str:
    """Compose the question for this attempt.

    When candidates are available the ask names them — recognition rather than
    recall. With no candidates to name the question is open. The same template
    is used on every attempt; a third ask is never sent.
    """
    if not candidates:
        return "I can mark that done. Which task did you mean?"

    titles = [candidate.title for candidate in candidates[:_CLARIFICATION_OPTION_LIMIT]]
    if attempts == 0:
        return f"I can mark that done — was it {_format_options(titles)}?"
    return f"Still not sure which one — was it {_format_options(titles)}?"


def _clarify_completion_target(
    peer: str,
    *,
    attempts: int = 0,
    candidates: tuple[DedupCandidate, ...] = (),
) -> dict[str, Any]:
    """Ask which task was meant, and remember having asked.

    `attempts` is the number of times this question has already gone out in the
    current exchange. Past _MAX_CLARIFICATION_ATTEMPTS the agent stops asking
    and leaves the tasks open — an unanswered question re-sent a third time
    costs the user attention and returns nothing.
    """
    if attempts >= _MAX_CLARIFICATION_ATTEMPTS:
        log.info(
            "complete_node.clarification_exhausted",
            has_peer=bool(peer),
            attempts=attempts,
            candidate_count=len(candidates),
        )
        give_up_draft: OutboundDraft = {
            "recipient": peer,
            "body": (
                "No problem — I'll leave those as they are. "
                "Send me the task name whenever you want it marked done."
            ),
            "notion_page_id": None,
        }
        return {
            "pending_outbound": [give_up_draft],
            "conversation_state": "idle",
            "active_task": None,
            "pending_clarification": None,
        }

    # Titles are the user's private words: store them in checkpointed state so
    # the re-ask can name them, and log counts only.
    stored: list[ClarificationCandidate] = [
        {"page_id": candidate.page_id, "title": candidate.title}
        for candidate in candidates[:_CLARIFICATION_OPTION_LIMIT]
    ]
    clarification: PendingClarification = {
        "kind": "complete_target",
        "asked_at": datetime.now(UTC).isoformat(),
        "attempts": attempts + 1,
        "candidates": stored,
    }
    no_task_draft: OutboundDraft = {
        "recipient": peer,
        "body": _clarification_body(attempts, candidates),
        "notion_page_id": None,
    }
    log.info(
        "complete_node.clarification_asked",
        has_peer=bool(peer),
        attempts=attempts + 1,
        named_option_count=len(stored),
    )
    return {
        "pending_outbound": [no_task_draft],
        "conversation_state": "idle",
        "active_task": None,
        "pending_clarification": clarification,
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

        # Attempts already spent on this question. Absent for a first "done",
        # present when this turn is the answer to a clarification classify_intent
        # kept alive.
        # classify_intent only carries a clarification forward while it is live,
        # so its presence means this turn is the answer to one. That changes how
        # the message reads: the completion claim was made on the previous turn
        # and this message only says which task it was about.
        pending = state.get("pending_clarification")
        answering = isinstance(pending, dict) and bool(pending)
        raw_attempts = pending.get("attempts", 0) if isinstance(pending, dict) else 0
        attempts = raw_attempts if isinstance(raw_attempts, int) and raw_attempts >= 0 else 0
        raw_offered = pending.get("candidates") if isinstance(pending, dict) else None
        offered: tuple[ClarificationCandidate, ...] = (
            tuple(raw_offered) if answering and isinstance(raw_offered, list) else ()
        )

        try:
            recent_target = await _load_recent_outbound_target(peer)
        except Exception:
            # Losing one source must not veto the others: the message may still
            # name the task outright, and that path does not touch Postgres.
            log.warning(
                "complete_node.recent_outbound_load_failed",
                active_page_id=active_target.page_id if active_target else None,
                exc_info=True,
            )
            recent_target = None

        # Only look the message up when it names something. "done!" resolves
        # from context alone and must not pay for a Notion read or a model call.
        residue = _task_reference_tokens(state.get("incoming") or "")
        # Answering a question we named options in earns the lookup on its own.
        # "the second one" reduces to no residue worth shortlisting, and it is
        # still a complete answer to what was asked.
        if residue or offered:
            title_match = await _resolve_title_match(
                state.get("incoming") or "",
                residue,
                now=now,
                answering_clarification=answering,
                offered=offered,
            )
        else:
            title_match = _TitleMatch(target=None, candidate_count=0, confidence=None)

        target = _choose_completion_target(
            active_target=active_target,
            recent_target=recent_target,
            title_target=title_match.target,
        )

        # When the message appeared to name a task (candidates the message
        # actually overlaps) but the model rejected all of them, the message
        # asserts something is NOT done — completing from stale context would
        # mark the wrong page.
        #
        # Scoped to the scored shortlist. A widened set contains every open
        # task, so a null match over it carries no claim about any particular
        # one: "done :) feeling good" would otherwise veto a live active task
        # on the strength of a candidate list the message never referred to.
        if (
            target
            and residue
            and not title_match.widened
            and title_match.candidate_count > 0
            and title_match.target is None
        ):
            return _clarify_completion_target(
                peer, attempts=attempts, candidates=title_match.candidates
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
            candidates_widened=title_match.widened,
            match_confidence=title_match.confidence,
            residue_token_count=len(residue),
            clarification_attempts=attempts,
            answering_clarification=answering,
        )

        if not target:
            return _clarify_completion_target(
                peer, attempts=attempts, candidates=title_match.candidates
            )

        page_id = target.page_id
        task_title = target.task_title

        if target.needs_notion_write:
            await notion.update_status(page_id=page_id, new_status="Completed")

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
                await _clear_recent_outbound(
                    peer=peer,
                    signal_timestamp=target.signal_timestamp,
                    notion_page_id=page_id,
                )
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
            "pending_clarification": None,
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
            "pending_clarification": None,
        }
