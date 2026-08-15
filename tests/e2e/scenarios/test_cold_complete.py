"""Scenario 6 — "done" with nothing to attribute it to.

The inverse of bug #641, and the case that gave the bug its name. With no
`active_task` in the checkpoint and no live `recent_outbound` row, the system has
no way to know which task the user finished. There are exactly two honest
behaviours: ask, or do nothing. Guessing is not one of them.

Before the fix this turn produced a canned "Done! Nice work." — a celebration for
a task nobody had identified, no Notion write, no reward, and no signal to the
user that nothing had actually been recorded.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_cold_complete_asks_instead_of_guessing(conversation: Conversation) -> None:
    # A pending task exists but was never offered to this peer. It is the
    # tempting wrong answer: the only Pending task in the database.
    unoffered = conversation.notion.seed_task(
        title="Call the dentist",
        work_type="Independent",
        urgency=90,
    )

    result = await conversation.say(
        "done",
        expect=Expect(
            intent="COMPLETE",
            notion_untouched=[unoffered],
            sent_count=1,
            regex_require=[r"(?i)which task"],
        ),
    )

    # Invariant I3 already forbids writing an unoffered page; assert the
    # stronger property that the turn wrote nothing at all.
    assert conversation.notion.writes[result.notion_writes_since :] == [], (
        "a COMPLETE turn with no resolvable target must not write to Notion"
    )
    assert conversation.notion.status_of(unoffered) == "Pending"
    assert result.state.get("active_task") is None

    # The draft must carry no page id — a clarifying question that quietly
    # attaches itself to a page would reward the wrong task on the next reply.
    drafts = result.state.get("pending_outbound") or []
    assert drafts and not drafts[0].get("notion_page_id")


async def test_stale_active_task_does_not_complete_the_wrong_page(
    conversation: Conversation,
) -> None:
    """A 48h-old checkpoint entry is expired context, not an answer.

    This is the second half of #641. Pre-fix, `complete_node` completed whatever
    `active_task` held with no freshness check at all, so a task offered days
    ago got marked done because the user said "done" about something else.
    """
    stale = conversation.notion.seed_task(title="Sort the mail", work_type="Independent")

    await conversation.seed_active_task(page_id=stale, title="Sort the mail")
    await conversation.age_active_task(hours=48)

    result = await conversation.say(
        "done",
        expect=Expect(
            intent="COMPLETE",
            notion_untouched=[stale],
            regex_require=[r"(?i)which task"],
        ),
    )

    assert conversation.notion.status_of(stale) == "Pending"
    assert result.state.get("active_task") is None


async def test_reminder_target_beats_stale_active_task(
    conversation: Conversation,
) -> None:
    """recent_outbound from reminder_worker takes precedence over a stale active_task.

    This is the exact seam #641 exposed: active_task holds <page_B> in the
    checkpoint; reminder_worker has written a recent_outbound row for <page_A>
    after sending a reminder. When the user replies "done", complete_node must
    resolve against <page_A> — not <page_B> — and must not touch <page_B> at all.
    """
    page_b = conversation.notion.seed_task(title="Buy stamps", work_type="Independent")
    page_a = conversation.notion.seed_task(
        title="Pay the water bill", work_type="Independent"
    )

    # Seed a stale active_task for page_B — older than the 24h TTL.
    await conversation.seed_active_task(page_id=page_b, title="Buy stamps")
    await conversation.age_active_task(hours=48)

    # A reminder was sent for page_A; reminder_worker wrote the recent_outbound row.
    await conversation.deliver_reminder(page_id=page_a, body="Test message")

    result = await conversation.say(
        "done",
        expect=Expect(
            intent="COMPLETE",
            notion_status={page_a: "Completed"},
            notion_untouched=[page_b],
            db_awaiting_reply=0,
        ),
    )

    assert result.state.get("active_task") is None
