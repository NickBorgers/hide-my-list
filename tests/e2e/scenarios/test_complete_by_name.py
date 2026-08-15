"""Scenario 11 — completing a task the message names.

The third resolution source, added for bug #655. Both context sources carry a
24h window, so once they lapse there was no path from the user's words to a
Notion page: "finished the dishes" got "Which task did you mean?" even with the
task sitting open on the list.

Worth stating plainly, because it shaped this file: #655 is the third instance
of the same bug class. #641 was the second. Each fix added a resolution source
without a test that watched the sources interact, and the next gap appeared in
the seam between them. These scenarios assert the interaction, not the source.

The lookup is additive by design — every failure in it (empty shortlist, null
match, confidence under 0.90, exception) falls through to the previous
resolution rather than replacing it. So the assertions here are about
precedence and about *not* firing, as much as about matching.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_a_named_task_resolves_with_no_context_at_all(
    conversation: Conversation,
) -> None:
    """No active_task, no reminder — the words alone have to carry it."""
    dishes = conversation.notion.seed_task(title="Wash the dishes", work_type="Independent")
    decoy = conversation.notion.seed_task(title="Book the dentist", work_type="Independent")

    # Nothing was offered this conversation, so the name match is the only
    # source that can produce a target. It must also be allowed to write the
    # page it matched, which invariant I3 would otherwise forbid.
    conversation.offered.add(dishes)

    await conversation.say(
        "finished washing the dishes",
        expect=Expect(
            intent="COMPLETE",
            notion_status={dishes: "Completed"},
            notion_untouched=[decoy],
            sent_count=1,
            regex_forbid=[r"(?i)which task"],
        ),
    )

    assert conversation.notion.status_of(decoy) == "Pending"


async def test_a_named_task_outranks_a_different_active_task(
    conversation: Conversation,
) -> None:
    """Precedence: the words beat the checkpoint when they disagree.

    An active_task pointing at a different page is exactly the case that would
    otherwise mark the wrong one done — the user names what they finished, and
    the system completes what it happened to be holding.
    """
    held = conversation.notion.seed_task(title="Vacuum the stairs", work_type="Independent")
    named = conversation.notion.seed_task(title="Water the garden", work_type="Independent")
    conversation.offered.add(named)

    await conversation.seed_active_task(page_id=held, title="Vacuum the stairs")

    await conversation.say(
        "just watered the garden",
        expect=Expect(
            intent="COMPLETE",
            notion_status={named: "Completed"},
            notion_untouched=[held],
            sent_count=1,
        ),
    )

    assert conversation.notion.status_of(held) == "Pending", (
        "the task the system was holding was completed instead of the one the "
        "user named"
    )


async def test_a_bare_done_still_uses_the_active_task(
    conversation: Conversation,
) -> None:
    """The additive guarantee: a message naming nothing takes the old path.

    "done" has no task in it, so the name lookup must not fire — no Notion read,
    no model call — and resolution must fall through to the checkpoint exactly
    as before #655. A regression that made the new path mandatory would turn
    every terse reply into a confidence gamble against the whole task list.
    """
    task = conversation.notion.seed_task(title="Empty the dishwasher", work_type="Independent")
    await conversation.seed_active_task(page_id=task, title="Empty the dishwasher")

    result = await conversation.say(
        "done", expect=Expect(intent="COMPLETE", notion_status={task: "Completed"}, sent_count=1)
    )

    callers = [
        entry.get("caller")
        for entry in result.logs
        if entry.get("event") == "llm.call.end"
    ]
    assert "complete_title_match" not in callers, (
        "the title-match lookup fired on a message that names no task; the cheap "
        "path exists so a bare 'done' costs no Notion read and no model call"
    )
