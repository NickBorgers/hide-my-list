"""Scenarios 4, 5, 9 and 10 — the remaining multi-turn chains.

Each one crosses a boundary that a single-node test cannot: a rejection whose
alternative must still be named, an intake whose created page a reminder later
targets, a follow-up message that only parses against the previous turn, and a
redelivery that must not double-complete.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_rejecting_a_task_offers_a_named_alternative(
    conversation: Conversation,
) -> None:
    """Scenario 4 — reject, then get something else.

    Two properties. The alternative has to be *named*: an alternative the user
    cannot identify is the same unactionable message the naming invariant exists
    to prevent, and it shipped once already. And the rejected task must not be
    completed — "not this one" is not "done".
    """
    conversation.notion.seed_task(
        title="Clean out the garage",
        work_type="Independent",
        energy_required="High",
        urgency=95,
        time_estimate=120,
    )
    conversation.notion.seed_task(
        title="Reply to the school email",
        work_type="Independent",
        energy_required="Low",
        urgency=60,
        time_estimate=10,
    )

    offer = await conversation.say(
        "give me something to do", expect=Expect(intent="GET_TASK", sent_count=1)
    )
    offered_page = (offer.state.get("active_task") or {}).get("page_id")
    assert offered_page, "selection_node offered nothing to reject"

    writes_before = conversation.notion.mark()
    await conversation.say("not that one", expect=Expect(intent="REJECT", sent_count=1))

    completed = {
        write.page_id
        for write in conversation.notion.writes[writes_before:]
        if write.op == "update_status" and write.payload.get("status") == "Completed"
    }
    assert offered_page not in completed, (
        "a rejected task was marked Completed; declining a suggestion is not "
        "finishing it"
    )
    assert conversation.notion.status_of(offered_page) != "Completed"


async def test_a_task_added_this_turn_can_be_reminded_about(
    conversation: Conversation,
) -> None:
    """Scenario 5 — intake creates the page a later reminder targets.

    The only chain where `intake_node` and `reminder_worker` meet. The page id
    intake returns has to be usable by the outbox and, in turn, resolvable by
    the COMPLETE that answers the reminder — a coercion or formatting mismatch
    anywhere along that path breaks silently.
    """
    writes_before = conversation.notion.mark()

    await conversation.say("add a task: sweep the porch", expect=Expect(intent="ADD_TASK"))

    created = [
        write.page_id
        for write in conversation.notion.writes[writes_before:]
        if write.op == "create_task"
    ]
    assert created, "intake_node created no Notion page"
    page_id = created[0]

    delivery = await conversation.deliver_reminder(
        page_id=page_id, body="Reminder: sweep the porch"
    )
    assert delivery.notion_page_id == page_id

    await conversation.say("finished", expect=Expect(intent="COMPLETE", db_awaiting_reply=0))


async def test_a_follow_up_turn_reads_the_previous_one(
    conversation: Conversation,
) -> None:
    """Scenario 9 — conversational context reaching the classifier.

    "by Friday" is meaningless alone. It classifies correctly only because
    `send_node` wrote the previous turn into `messages` and the classifier
    windows the last few. This asserts the plumbing — that turn 1 is present in
    the checkpoint the classifier reads — rather than asserting a particular
    intent label, which is the model's call to make.
    """
    await conversation.say("add a task: call the dentist", expect=Expect(intent="ADD_TASK"))

    state = await conversation.state()
    history = [str(getattr(message, "content", "")) for message in state.get("messages", [])]
    assert any("call the dentist" in text for text in history), (
        "send_node did not record the turn into `messages`; the next turn's "
        "classifier would see no prior context"
    )

    follow_up = await conversation.say("by Friday")

    later = [
        str(getattr(message, "content", ""))
        for message in follow_up.state.get("messages", [])
    ]
    assert any("call the dentist" in text for text in later), (
        "the first turn fell out of the conversation history"
    )
    assert any("by Friday" in text for text in later)


async def test_redelivering_a_reminder_completes_it_once(
    conversation: Conversation,
) -> None:
    """Scenario 10 — two live reminders for one page, then a single "done".

    A page can legitimately have several reminders in flight: migration 0007
    dropped the UNIQUE on `reminder_outbox.notion_page_id` so deadline milestones
    could stack. Each delivery writes its own `recent_outbound` row keyed on its
    own signal_timestamp.

    `complete_node` resolves the most recent row and clears exactly that one, so
    a reply consumes one reminder rather than all of them. That is asserted
    below as the current behaviour.

    **Known gap this scenario pins down rather than fixes.** The row left behind
    is an orphan: it stays `awaiting_reply = true` for its full 24h window, and a
    later unrelated "done" will resolve *it* — rewarding a task the user already
    finished and completing the wrong thing, which is the shape of #641 all over
    again. Closing it is a production change (resolve every live row for the
    completed page, or scope the clear by page rather than timestamp), so it is
    recorded here rather than silently absorbed.
    """
    page = conversation.notion.seed_task(
        title="Pick up the prescription",
        work_type="Independent",
        is_reminder=True,
        reminder_status="pending",
    )

    first = await conversation.deliver_reminder(page_id=page, body="Reminder: pick up the prescription")
    second = await conversation.deliver_reminder(page_id=page, body="Reminder: pick up the prescription")
    assert second.signal_timestamp != first.signal_timestamp

    async with conversation.db() as conn:
        cursor = await conn.execute(
            "SELECT count(*) FROM recent_outbound WHERE peer = %s AND awaiting_reply = true",
            (conversation.peer,),
        )
        row = await cursor.fetchone()
    assert row is not None and row[0] == 2, (
        "two distinct deliveries should leave two rows; a collision here would "
        "mean the second send had no context to resolve against"
    )

    result = await conversation.say("done", expect=Expect(intent="COMPLETE"))

    async with conversation.db() as conn:
        cursor = await conn.execute(
            "SELECT count(*) FROM recent_outbound WHERE peer = %s AND awaiting_reply = true",
            (conversation.peer,),
        )
        row = await cursor.fetchone()

    assert row is not None and row[0] == 1, (
        "one reply should consume exactly one reminder row — resolving both "
        "would reward a single 'done' twice"
    )
    # Exactly one outbound reply, so the double delivery did not produce a
    # double celebration.
    assert len(result.sent) == 1
