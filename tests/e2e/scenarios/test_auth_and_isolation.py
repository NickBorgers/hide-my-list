"""Scenarios 7 and 8 — checkpoint partitioning and the authorization gate.

Both are properties of the ingress layer, which is why the harness enters
through `SignalListener` rather than `graph.ainvoke`. A test that invoked the
graph directly would supply `thread_id` itself and could not observe either.

Scenario 7 supersedes `tests/spike/test_thread_isolation.py`, a phase-B artifact
that no workflow ran and that — with no proxy env — passed entirely through
exception fallbacks while attempting real outbound HTTP.
"""
from __future__ import annotations

import pytest

from tests.support.harness import Conversation, Expect

pytestmark = pytest.mark.asyncio


async def test_unauthorized_peer_is_dropped_before_the_graph(
    conversation: Conversation,
) -> None:
    """An unlisted number must not reach the graph, Notion, or Signal.

    Notion is single-tenant: any peer that gets through can read every task the
    user has. The gate is the only thing standing between an inbound message and
    that database.
    """
    task = conversation.notion.seed_task(title="Renew the car registration")

    await conversation.seed_active_task(page_id=task, title="Renew the car registration")
    checkpoint_before = await conversation.state()

    sent_before = conversation.signal.mark()
    writes_before = conversation.notion.mark()

    result = await conversation.unauthorized("done", peer="+15559999999")

    assert not result.graph_invoked, "an unauthorized peer reached the graph"
    assert conversation.signal.since(sent_before) == [], (
        "an unauthorized peer received a reply"
    )
    assert conversation.notion.writes[writes_before:] == [], (
        "an unauthorized peer caused a Notion write"
    )
    assert conversation.notion.status_of(task) == "Pending"

    # The authorized peer's own conversation must be untouched — an intruder
    # cannot consume the context the real user is mid-way through.
    assert await conversation.state() == checkpoint_before

    dropped = [entry.get("event") for entry in result.logs]
    assert "signal_listener.unauthorized_peer_dropped" in dropped


async def test_the_authorized_peer_still_works_after_an_intrusion(
    conversation: Conversation,
) -> None:
    """The gate must drop the message, not wedge the listener.

    A `continue` that skipped cleanup, or an exception escaping the auth check,
    would leave the consumer loop dead and every subsequent message unanswered —
    silently, because the listener logs and keeps going.
    """
    task = conversation.notion.seed_task(title="Defrost the freezer")
    await conversation.seed_active_task(page_id=task, title="Defrost the freezer")

    await conversation.unauthorized("hello?", peer="+15559999999")

    await conversation.say(
        "done",
        expect=Expect(intent="COMPLETE", notion_status={task: "Completed"}, sent_count=1),
    )


async def test_two_peers_keep_separate_checkpoints(conversation_pair: tuple) -> None:
    """`thread_id` is the peer E.164, so one listener serves disjoint threads.

    Interleaved, because a shared-state bug that a sequential test would miss
    shows up when turn 2 of peer A lands between turns 1 and 2 of peer B.
    """
    first, second = conversation_pair

    first_task = first.notion.seed_task(title="Change the air filter")
    second_task = second.notion.seed_task(title="Send the rent cheque")

    await first.seed_active_task(page_id=first_task, title="Change the air filter")
    await second.seed_active_task(page_id=second_task, title="Send the rent cheque")

    await first.say("done", expect=Expect(intent="COMPLETE", notion_status={first_task: "Completed"}, sent_count=1))

    # The second peer's checkpoint must be untouched by the first peer's turn.
    second_state = await second.state()
    assert (second_state.get("active_task") or {}).get("page_id") == second_task, (
        "one peer's COMPLETE cleared another peer's active task"
    )
    assert second.notion.status_of(second_task) == "Pending"

    await second.say(
        "done", expect=Expect(intent="COMPLETE", notion_status={second_task: "Completed"}, sent_count=1)
    )

    assert (await first.state()).get("active_task") is None

    # One sink serves both peers, as one signal-cli account does in production.
    # Every message must still be addressed to the peer whose turn produced it.
    recipients = {message.recipient for message in first.signal.sent}
    assert recipients == {first.peer, second.peer}, (
        f"messages went to unexpected recipients: {recipients}"
    )
