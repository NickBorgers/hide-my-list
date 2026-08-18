"""Scenario 12 — a paraphrase, and a question the agent remembers asking.

The fourth instance of the resolution bug class (#641, #655, then this). The
pattern each time: a source is added, and the seam around it goes untested. Here
the source added by #655 was real but unreachable — a token-overlap shortlist
stood in front of it and returned an empty candidate set for any message that
described a task instead of quoting it, so the model that was supposed to
adjudicate never saw the list.

Production, three turns, 35 seconds:

    "which task did you mean?" → user answers → same question → user answers
    → same question

Byte-identical each time, because the reply cleared `conversation_state` and
`active_task` and recorded nothing about having asked.

These scenarios are cross-turn on purpose. A single-turn test can prove the
model got the list; only a chain can prove the second turn behaves differently
from the first, which is the half the user actually experienced.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_a_paraphrase_resolves_without_quoting_the_title(
    conversation: Conversation,
) -> None:
    """The production message shape: the user's words, not the task's words.

    "the fridge in the garage" against "Deal with the spare refrigerator in the
    basement" shares no token the shortlist scores — the words a person reaches
    for describing their own task are rarely the words they filed it under.
    """
    fridge = conversation.notion.seed_task(
        title="Deal with the spare refrigerator in the basement",
        work_type="Physical",
    )
    decoy = conversation.notion.seed_task(title="Book the dentist", work_type="Independent")
    conversation.offered.add(fridge)

    await conversation.say(
        "finally got that garage fridge sorted out",
        expect=Expect(
            intent="COMPLETE",
            notion_status={fridge: "Completed"},
            notion_untouched=[decoy],
            sent_count=1,
            regex_forbid=[r"(?i)which task"],
        ),
    )

    assert conversation.notion.status_of(decoy) == "Pending"


async def test_the_answer_to_the_question_lands_on_the_task(
    conversation: Conversation,
) -> None:
    """Turn 1 asks. Turn 2 answers. Turn 2 must complete something.

    The answer to "which task did you mean?" is a bare noun phrase — not a
    completion sentence, so the classifier has no reason to call it COMPLETE on
    its own. Routing it back to the node that asked is the whole point of
    remembering the question.
    """
    garden = conversation.notion.seed_task(title="Water the garden", work_type="Physical")
    conversation.offered.add(garden)

    first = await conversation.say(
        "yep, knocked that one out",
        expect=Expect(
            intent="COMPLETE",
            notion_untouched=[garden],
            sent_count=1,
            regex_require=[r"(?i)which task"],
        ),
    )
    assert first.state.get("pending_clarification"), (
        "the question has to survive the turn that asked it"
    )

    second = await conversation.say(
        "the garden one",
        expect=Expect(
            notion_status={garden: "Completed"},
            sent_count=1,
            regex_forbid=[r"(?i)which task"],
        ),
    )
    assert second.state.get("pending_clarification") is None


async def test_the_agent_stops_repeating_itself(conversation: Conversation) -> None:
    """Three unresolvable turns must produce three different replies, then stop.

    This is the user-visible bug stated directly. The catch is that "stops
    asking" and "keeps asking" both write nothing to Notion, so a side-effect
    assertion cannot tell them apart — the reply text is the only place the
    difference shows.
    """
    task = conversation.notion.seed_task(title="Water the garden", work_type="Physical")

    bodies = []
    for message in ("yeah did that", "the other thing", "you know the one"):
        turn = await conversation.say(
            message,
            expect=Expect(notion_untouched=[task], sent_count=1),
        )
        bodies.append(turn.text)

    assert len(set(bodies)) == 3, "the same question three times is the bug"
    assert "which task" not in bodies[-1].lower(), (
        "after the cap the agent leaves the tasks open rather than asking again"
    )
