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

    "old fridge" against "Deal with the spare refrigerator" shares no token the
    shortlist scores — the words a person reaches for describing their own task
    are rarely the words they filed it under.

    What this asserts is that zero word overlap no longer ends the turn, and
    the pair is deliberately one synonym wide so it stays a test of that rather
    than of how far the cheap tier can reach. Widening guarantees the model is
    asked; it does not guarantee the model can bridge any distance, and a
    scenario that demanded a harder leap would be measuring the model.
    """
    fridge = conversation.notion.seed_task(
        title="Deal with the spare refrigerator",
        work_type="Physical",
    )
    decoy = conversation.notion.seed_task(title="Book the dentist", work_type="Independent")
    conversation.offered.add(fridge)

    await conversation.say(
        "got rid of that old fridge finally",
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
    its own. Routing it back to the node that asked is half the point of
    remembering the question.

    The other half is that the node judges it as an answer. The standalone
    matching rule rejects anything that does not assert a task is finished, and
    "the garden one" never will: the assertion was made on turn 1. Route
    without reframing and the reply comes back rejected, which looks from the
    outside exactly like the bug this scenario exists to catch.
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
    assert second.state.get("pending_clarification") is None, (
        "a resolved question must not survive into the next turn"
    )
    assert second.state.get("active_task") is None
    assert second.state.get("conversation_state") == "idle"


async def test_a_positional_answer_resolves_the_option_it_points_at(
    conversation: Conversation,
) -> None:
    """Offering "was it A or B?" has to make "the first one" a usable reply.

    Constrained choices exist to spend less of the user's working memory
    (`design/adhd-priorities.md`). Naming options and then requiring the title
    typed back inverts that: it invites the short answer and demands the long
    one. The assertion reads the offered order out of the checkpoint rather
    than assuming it, because ranking decides which task is named first.
    """
    first = conversation.notion.seed_task(title="Water the garden", work_type="Physical")
    second = conversation.notion.seed_task(title="Sweep the porch", work_type="Physical")
    conversation.offered.update({first, second})

    await conversation.say(
        "yeah did that",
        expect=Expect(notion_untouched=[first, second], sent_count=1),
    )
    named = await conversation.say(
        "the other thing",
        expect=Expect(notion_untouched=[first, second], sent_count=1),
    )

    options = (named.state.get("pending_clarification") or {}).get("candidates") or []
    assert len(options) >= 2, "a positional answer needs at least two options named"
    expected = options[0]["page_id"]

    await conversation.say("the first one", expect=Expect(sent_count=1))

    assert conversation.notion.status_of(expected) == "Completed"
    other = second if expected == first else first
    assert conversation.notion.status_of(other) == "Pending"


async def test_the_agent_stops_repeating_itself(conversation: Conversation) -> None:
    """Three unresolvable turns must produce three different replies, then stop.

    This is the user-visible bug stated directly. The catch is that "stops
    asking" and "keeps asking" both write nothing to Notion, so a side-effect
    assertion cannot tell them apart — the reply text is the only place the
    difference shows.
    """
    task = conversation.notion.seed_task(title="Water the garden", work_type="Physical")

    turns = []
    for message in ("yeah did that", "the other thing", "you know the one"):
        turns.append(
            await conversation.say(
                message,
                expect=Expect(notion_untouched=[task], sent_count=1),
            )
        )

    bodies = [turn.text for turn in turns]
    assert len(set(bodies)) == 3, "the same question three times is the bug"
    assert "which task" not in bodies[-1].lower(), (
        "after the cap the agent leaves the tasks open rather than asking again"
    )

    # The checkpoint is what the next turn reads, and it is the half the
    # delivered text cannot show. A give-up turn that leaves the clarification
    # live would look identical here and then pull the next ordinary message
    # through COMPLETE.
    assert turns[0].state.get("pending_clarification"), "turn 1 has to record the question"
    assert turns[1].state.get("pending_clarification"), "turn 2 is still asking"
    assert turns[2].state.get("pending_clarification") is None, (
        "the handoff terminates: nothing outstanding survives the give-up turn"
    )
    assert turns[2].state.get("active_task") is None
    assert turns[2].state.get("conversation_state") == "idle"

    # And the next ordinary message must route on its own merits again.
    after = await conversation.say("thanks", expect=Expect(sent_count=1))
    assert after.intent != "COMPLETE", (
        "a cleared clarification must stop steering the following turn"
    )
    assert conversation.notion.status_of(task) == "Pending"
