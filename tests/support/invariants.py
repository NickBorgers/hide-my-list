"""Cross-cutting invariants, checked after every conversation turn.

These are what make the layer catch a *class* of failure rather than one bug.
A scenario asserts what that scenario is about; the invariants assert what must
hold on every turn of every scenario, so a regression in an unrelated chain
still trips as soon as any scenario walks past it.

Every check here is deterministic under a nondeterministic model. None of them
reads the model's phrasing — they read which page was written, whether a
reminder was resolved, how many messages went out, and which structlog events
fired. That is the property that lets a real-LLM suite gate a merge without
flaking.

  I1  no node silently took its exception fallback
  I2  a draft naming a task delivers that task's name
  I3  no Notion write targets a page the peer was never offered
  I4  a COMPLETE turn resolves the reminders it claimed to
  I5  outbound sends are idempotent within a conversation
  I6  delivered text carries no banned shame phrase
  I7  each LLM caller used its documented tier
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from tests.support.shame import find_banned_phrases

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tests.support.harness import Conversation, Expect, TurnResult

# I1. A node that catches an exception logs one of these and then returns a
# hand-written, shame-safe fallback. The fallback reads fine, which is exactly
# the problem: without this check a broken chain scores green on tone while
# having done nothing. Default-deny; a scenario opts in via Expect.allow_events.
_ERROR_EVENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^[a-z_]+_node\.error$"),
    re.compile(r"^classify_intent\.error$"),
    re.compile(r"^signal_listener\.graph_error$"),
    re.compile(r"_failed$"),
)

# I7. Tier assignments from app/models.py. A node quietly downgraded to `cheap`
# gets think=False and max_tokens=1024, which truncates intake's JSON mid-object
# and drops the task's reminder without raising anything.
_EXPECTED_TIERS: dict[str, str] = {
    "classify": "cheap",
    "complete_title_match": "cheap",
    "intake_dedup": "cheap",
    "selection": "expensive",
}
_DEFAULT_TIER = "medium"


def _event_name(entry: dict[str, Any]) -> str:
    return str(entry.get("event", ""))


def assert_turn_invariants(
    conv: Conversation, result: TurnResult, expect: Expect
) -> None:
    """Run every invariant against one completed turn."""
    _assert_no_error_events(result, expect)
    _assert_task_naming(result)
    _assert_no_unoffered_write(conv, result)
    _assert_awaiting_reply_resolution(result)
    _assert_send_idempotency(conv, expect)
    _assert_shame_safe(result)
    _assert_tier_discipline(result)


# ---------------------------------------------------------------------------
# I1 — no silent exception fallback
# ---------------------------------------------------------------------------


def _assert_no_error_events(result: TurnResult, expect: Expect) -> None:
    offenders = [
        _event_name(entry)
        for entry in result.logs
        if _event_name(entry) not in expect.allow_events
        and any(pattern.search(_event_name(entry)) for pattern in _ERROR_EVENT_PATTERNS)
    ]
    assert not offenders, (
        f"a node took its exception fallback this turn: {sorted(set(offenders))}. "
        "The reply may still read well — that is what makes this failure mode "
        "invisible without the check. Add the event to Expect.allow_events only "
        "if the scenario is deliberately exercising the failure path."
    )


# ---------------------------------------------------------------------------
# I2 — task-naming invariant
# ---------------------------------------------------------------------------


def _assert_task_naming(result: TurnResult) -> None:
    """A draft carrying `notion_page_title` must deliver that title.

    `send_node` guarantees this in code by applying `render_task_token` before
    hashing, so the check holds regardless of what the model wrote. It catches a
    regression in that chokepoint, which last shipped as a suggestion the user
    could not act on because it named no task.
    """
    for message in result.sent:
        assert "{task}" not in message.body, (
            "an unsubstituted {task} token was delivered — render_task_token did "
            "not run on this draft"
        )
        assert "[task]" not in message.body, (
            "a literal [task] placeholder was delivered"
        )

    drafts = result.state.get("pending_outbound", []) or []
    delivered = " \n".join(message.body for message in result.sent)
    for draft in drafts:
        title = draft.get("notion_page_title")
        if not title or not draft.get("body"):
            continue
        assert title in delivered, (
            f"a draft declared notion_page_title={title!r} but no delivered message "
            "names it; the user cannot act on a task they were not told the name of"
        )


# ---------------------------------------------------------------------------
# I3 — no write to an unoffered page
# ---------------------------------------------------------------------------


def _assert_no_unoffered_write(conv: Conversation, result: TurnResult) -> None:
    """Mutating a page the peer was never shown is always wrong.

    This is the generalized form of #641's worst branch, where a stale
    `active_task` caused the wrong Notion page to be marked Completed while the
    reminder the user was actually answering went unresolved. Whatever the
    intent, a page nobody was offered is not a page the user just finished.
    """
    mutations = {"update_status", "update_property", "complete_reminder"}
    written = {
        write.page_id
        for write in conv.notion.writes[result.notion_writes_since :]
        if write.op in mutations
    }
    stray = written - conv.offered
    assert not stray, (
        f"this turn mutated Notion pages the peer was never offered: {sorted(stray)}. "
        f"Offered so far: {sorted(conv.offered)}"
    )


# ---------------------------------------------------------------------------
# I4 — awaiting_reply resolution
# ---------------------------------------------------------------------------


def _assert_awaiting_reply_resolution(result: TurnResult) -> None:
    """A COMPLETE turn must consume a reminder that was awaiting a reply.

    Resolving nothing is the first half of #641: the row stays live and the next
    "done" resolves the same stale reminder again. Any other intent must not
    silently clear context it did not answer.

    The check is "zero", including when a page has several reminders in flight.
    Migration 0007 dropped the UNIQUE on `reminder_outbox.notion_page_id` so
    deadline milestones could stack, and each delivery writes its own
    `recent_outbound` row, so `_clear_recent_outbound` resolves by page rather
    than by the single delivery the user happened to answer. Any row it leaves
    behind is an orphan a later unrelated "done" can be misattributed to — the
    wrong task completed, and a reward celebrating work already finished, which
    `docs/reward-system.md` forbids.
    """
    if result.intent == "COMPLETE" and result.awaiting_reply_before > 0:
        assert result.awaiting_reply_after == 0, (
            f"a COMPLETE turn left {result.awaiting_reply_after} reminder(s) awaiting "
            f"a reply (was {result.awaiting_reply_before}); each one is an orphan a "
            "later 'done' can resolve, completing the wrong task and rewarding work "
            "that was already celebrated"
        )
    elif result.intent != "COMPLETE":
        assert result.awaiting_reply_after >= result.awaiting_reply_before, (
            f"a {result.intent} turn cleared reminder context it did not answer "
            f"({result.awaiting_reply_before} -> {result.awaiting_reply_after})"
        )


# ---------------------------------------------------------------------------
# I5 — outbound idempotency
# ---------------------------------------------------------------------------


def _assert_send_idempotency(conv: Conversation, expect: Expect) -> None:
    """`send_node` keys on sha256(recipient:body), so a repeat body collides.

    Checked across the whole conversation rather than one turn: the observable
    of #641's double-celebration is the *second* identical message, several
    turns after the first.
    """
    graph_sends = [message for message in conv.signal.sent if message.idempotency_key]
    if not expect.allow_duplicate_send:
        keys = [(message.recipient, message.idempotency_key) for message in graph_sends]
        duplicates = {key for key in keys if keys.count(key) > 1}
        assert not duplicates, (
            f"the same message was sent twice in this conversation: {sorted(duplicates)}"
        )


# ---------------------------------------------------------------------------
# I6 — shame safety
# ---------------------------------------------------------------------------


def _assert_shame_safe(result: TurnResult) -> None:
    """Scored on delivered text, after token substitution — what the user reads."""
    for message in result.sent:
        found = find_banned_phrases(message.body)
        assert not found, f"delivered message contains banned shame phrases: {found}"


# ---------------------------------------------------------------------------
# I7 — tier discipline
# ---------------------------------------------------------------------------


def _assert_tier_discipline(result: TurnResult) -> None:
    for entry in result.logs:
        if _event_name(entry) != "llm.call.end":
            continue
        caller = str(entry.get("caller") or "")
        if not caller:
            continue
        expected = _EXPECTED_TIERS.get(caller, _DEFAULT_TIER)
        actual = str(entry.get("tier") or "")
        assert actual == expected, (
            f"caller {caller!r} used the {actual!r} tier; app/models.py assigns it "
            f"{expected!r}. A downgrade to 'cheap' also turns reasoning off and caps "
            "output at 1024 tokens, which truncates structured JSON mid-object."
        )


# ---------------------------------------------------------------------------
# Per-scenario expectations
# ---------------------------------------------------------------------------


def assert_expectations(conv: Conversation, result: TurnResult, expect: Expect) -> None:
    """Check this scenario's own contract for the turn."""
    from tests.support.harness import IntentMisrouteError

    if expect.intent is not None and result.intent != expect.intent:
        raise IntentMisrouteError(
            f"turn declared {expect.intent}, classifier returned {result.intent!r}"
        )

    for page_id, status in expect.notion_status.items():
        actual = conv.notion.status_of(page_id)
        assert actual == status, (
            f"page {page_id} is {actual!r}, expected {status!r}"
        )

    for page_id in expect.notion_untouched:
        touched = {
            write.page_id
            for write in conv.notion.writes[result.notion_writes_since :]
        }
        assert page_id not in touched, (
            f"page {page_id} was written this turn but the scenario expects it untouched"
        )

    if expect.db_awaiting_reply is not None:
        assert result.awaiting_reply_after == expect.db_awaiting_reply, (
            f"{result.awaiting_reply_after} reminder(s) awaiting a reply, "
            f"expected {expect.db_awaiting_reply}"
        )

    if expect.sent_count is not None:
        assert len(result.sent) == expect.sent_count, (
            f"{len(result.sent)} message(s) sent, expected {expect.sent_count}: "
            f"{[m.body[:60] for m in result.sent]}"
        )

    for pattern in expect.regex_require:
        assert re.search(pattern, result.text), (
            f"delivered text does not match required pattern {pattern!r}"
        )

    for pattern in expect.regex_forbid:
        assert not re.search(pattern, result.text), (
            f"delivered text matches forbidden pattern {pattern!r}"
        )
