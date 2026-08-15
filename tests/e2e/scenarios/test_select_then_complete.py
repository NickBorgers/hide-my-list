"""Scenario 3 — ask for a task, then say it is done.

The baseline chain: `selection_node` writes `active_task` to the checkpoint on
turn 1, and `complete_node` reads it on turn 2. It is the only path that reaches
`notion.update_status(page_id, "Completed")`, so it guards the one place a task
actually gets closed.

Turn 1 also exercises the naming invariant end to end. `selection_node` puts the
stored title on the draft and `send_node` substitutes it, so the delivered
message must name the task the user is being asked to do — a suggestion the user
cannot act on is the failure this chain last shipped.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_selected_task_can_be_completed(conversation: Conversation) -> None:
    laundry = conversation.notion.seed_task(
        title="Fold the laundry",
        work_type="Independent",
        energy_required="Low",
        urgency=80,
        time_estimate=15,
    )

    offer = await conversation.say(
        "what should I work on?",
        expect=Expect(intent="GET_TASK", sent_count=1),
    )

    # selection_node marks the offered task In Progress and records it in the
    # checkpoint. Both halves matter: the status is what a later query_pending
    # filters on, the checkpoint entry is what the next turn resolves against.
    assert conversation.notion.status_of(laundry) == "In Progress"
    active = offer.state.get("active_task") or {}
    assert active.get("page_id") == laundry
    assert active.get("selected_at"), (
        "selection_node must stamp selected_at; without it complete_node cannot "
        "judge whether the entry is stale and falls through to asking which task"
    )
    assert "Fold the laundry" in offer.text

    done = await conversation.say(
        "done",
        expect=Expect(
            intent="COMPLETE",
            notion_status={laundry: "Completed"},
            sent_count=1,
            regex_forbid=[r"(?i)which task"],
        ),
    )

    # The checkpoint entry must be cleared, or the next "done" would complete
    # this same page a second time.
    assert done.state.get("active_task") is None
    assert done.state.get("streak", 0) >= 1
