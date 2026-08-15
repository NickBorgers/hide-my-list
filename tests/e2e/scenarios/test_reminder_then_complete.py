"""Scenario 1 and 2 — completing the task a delivered reminder was about.

This is bug #641 itself. The reminder is delivered by `reminder_worker`, which
is the only writer of `recent_outbound`; the user replies "done" some turns
later; `complete_node` has to resolve that reply against the reminder row rather
than against whatever happens to be sitting in the checkpoint.

Pre-fix there was no `recent_outbound` path at all, so this chain produced a
canned celebration with no reward, no page cleared, and the row left
`awaiting_reply = true` — meaning the *next* "done" would resolve the same
reminder a second time.

The reminder travels through production code deliberately. A fixture
`INSERT INTO recent_outbound` here would keep passing with the worker's INSERT
deleted, which is precisely the state of the world these scenarios exist to
prevent returning to.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_done_after_a_reminder_resolves_that_reminder(
    conversation: Conversation,
) -> None:
    page = conversation.notion.seed_task(
        title="Take the recycling out",
        work_type="Independent",
        is_reminder=True,
        reminder_status="pending",
    )

    delivery = await conversation.deliver_reminder(
        page_id=page, body="Reminder: take the recycling out"
    )
    assert await conversation.awaiting_reply_count() == 1

    result = await conversation.say(
        "did it",
        expect=Expect(
            intent="COMPLETE",
            db_awaiting_reply=0,
            sent_count=1,
            regex_forbid=[r"(?i)which task"],
        ),
    )

    # The reminder page is completed by the worker at delivery time
    # (complete_reminder), not by this turn — so the turn's job is to resolve
    # the outbox row and reward, not to write Notion again.
    async with conversation.db() as conn:
        cursor = await conn.execute(
            "SELECT awaiting_reply FROM recent_outbound WHERE peer = %s AND signal_timestamp = %s",
            (conversation.peer, delivery.signal_timestamp),
        )
        row = await cursor.fetchone()

    assert row is not None and row[0] is False, (
        "the reminder row is still awaiting a reply; the next 'done' would "
        "resolve this same reminder again"
    )
    assert result.state.get("active_task") is None


async def test_reminder_wins_over_a_stale_active_task(
    conversation: Conversation,
) -> None:
    """The worst branch of #641: completing the wrong page.

    A task offered two days ago is still sitting in the checkpoint when a
    reminder about a *different* page is delivered. Pre-fix, `complete_node`
    unconditionally completed the checkpoint's page — so the stale task was
    marked done and the reminder the user was actually answering stayed open.

    Both halves are asserted: the stale page must be untouched, and the reminder
    row must be resolved.
    """
    stale = conversation.notion.seed_task(title="Water the plants", work_type="Independent")
    reminder_page = conversation.notion.seed_task(
        title="Move the laundry over",
        work_type="Independent",
        is_reminder=True,
        reminder_status="pending",
    )

    await conversation.seed_active_task(page_id=stale, title="Water the plants")
    await conversation.age_active_task(hours=48)

    delivery = await conversation.deliver_reminder(
        page_id=reminder_page, body="Reminder: move the laundry over"
    )

    await conversation.say(
        "done",
        expect=Expect(
            intent="COMPLETE",
            notion_untouched=[stale],
            db_awaiting_reply=0,
        ),
    )

    assert conversation.notion.status_of(stale) == "Pending", (
        "a task offered 48 hours ago was marked Completed because the user said "
        "'done' about a reminder for a different task"
    )

    async with conversation.db() as conn:
        cursor = await conn.execute(
            "SELECT awaiting_reply FROM recent_outbound WHERE peer = %s AND signal_timestamp = %s",
            (conversation.peer, delivery.signal_timestamp),
        )
        row = await cursor.fetchone()
    assert row is not None and row[0] is False


async def test_the_more_recent_context_wins(conversation: Conversation) -> None:
    """A *fresh* active_task competing with a newer reminder.

    Distinct from the stale case above, and the only one that exercises the
    arbitration itself: when both candidates are live, `complete_node` compares
    their timestamps and the more recent context wins. With the stale task the
    24h TTL disqualifies the checkpoint entry before arbitration is reached, so
    a regression to checkpoint-first ordering would slip past that scenario.

    The user is answering the thing that just arrived, not the thing they were
    offered an hour ago.
    """
    offered_earlier = conversation.notion.seed_task(
        title="Book the eye appointment", work_type="Independent"
    )
    reminder_page = conversation.notion.seed_task(
        title="Put the bins out",
        work_type="Independent",
        is_reminder=True,
        reminder_status="pending",
    )

    # Fresh enough to survive the TTL, older than the reminder that follows.
    await conversation.seed_active_task(
        page_id=offered_earlier, title="Book the eye appointment"
    )
    await conversation.age_active_task(hours=1)

    delivery = await conversation.deliver_reminder(
        page_id=reminder_page, body="Reminder: put the bins out"
    )

    await conversation.say(
        "done",
        expect=Expect(
            intent="COMPLETE",
            notion_untouched=[offered_earlier],
            db_awaiting_reply=0,
        ),
    )

    assert conversation.notion.status_of(offered_earlier) == "Pending", (
        "the task offered an hour ago was completed instead of the reminder that "
        "arrived a moment ago"
    )

    async with conversation.db() as conn:
        cursor = await conn.execute(
            "SELECT awaiting_reply FROM recent_outbound WHERE peer = %s AND signal_timestamp = %s",
            (conversation.peer, delivery.signal_timestamp),
        )
        row = await cursor.fetchone()
    assert row is not None and row[0] is False
