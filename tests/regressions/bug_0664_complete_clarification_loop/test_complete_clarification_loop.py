"""Regression: COMPLETE must consult the model, and must remember asking (#664).

Production shape, three consecutive turns 35 seconds apart, all classified
COMPLETE, all logging a fully null `complete_node.resolved_target`, all sending
a byte-identical body (one shared idempotency key across the three sends):

    turn 1  residue 0   no Notion read       candidate_count 0
    turn 2  residue 5   Notion read          candidate_count 0
    turn 3  residue 6   Notion read          candidate_count 0

Turns 2 and 3 are the bug. The user answered "which task did you mean?" twice,
each time with more words, and the token-overlap shortlist scored every open
task below `_TITLE_MATCH_MIN_SCORE`, so `_resolve_title_match` returned before
building a prompt. The model was never shown the list it was there to
adjudicate. Then the reply cleared `conversation_state` and `active_task`,
leaving no record that a question had been asked, so the next turn re-entered
cold and failed identically.

Two halves, and both must hold:

  - a lexical score may rank the candidate set, never veto it into existence
  - a question the agent asks has to survive into the turn that answers it

Neither half may lower the bar on the destructive half of this node: the model
still has to pick from the candidates and clear
`_TITLE_MATCH_CONFIDENCE_THRESHOLD` before Notion is written.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.graph import routing
from app.graph.nodes import complete as complete_module
from app.graph.state import State


def _notion_page(page_id: str, title: str, status: str = "Pending") -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Status": {"select": {"name": status}},
            "Is Reminder": {"checkbox": False},
        },
    }


def _model(content: str) -> AsyncMock:
    response = MagicMock()
    response.content = content
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=response)
    return model


def _state(
    incoming: str,
    *,
    active_task: dict[str, Any] | None = None,
    pending_clarification: dict[str, Any] | None = None,
) -> State:
    return {  # type: ignore[return-value]
        "peer": "<test-peer>",
        "incoming": incoming,
        "intent": "COMPLETE",
        "messages": [],
        "active_task": active_task,
        "streak": 1,
        "tasks_completed_today": 0,
        "user_prefs": {},
        "mood": None,
        "available_minutes": None,
        "conversation_state": "idle",
        "pending_outbound": [],
        "pending_clarification": pending_clarification,
    }


def _live_active(page_id: str, title: str) -> dict[str, Any]:
    return {
        "page_id": page_id,
        "title": title,
        "selected_at": datetime.now(UTC).isoformat(),
        "work_type": "Physical",
        "energy_required": "Low",
    }


def _clarification(attempts: int, *, age: timedelta = timedelta(0)) -> dict[str, Any]:
    return {
        "kind": "complete_target",
        "asked_at": (datetime.now(UTC) - age).isoformat(),
        "attempts": attempts,
        "candidates": [
            {"page_id": "<page_A>", "title": "Deal with the spare fridge"},
            {"page_id": "<page_B>", "title": "Book the dentist appointment"},
        ],
    }


# ---------------------------------------------------------------------------
# Half one: the score ranks, it does not veto
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_paraphrase_sharing_no_words_still_reaches_the_model() -> None:
    """The production failure. Zero token overlap must not mean zero candidates.

    "cleaned out the garage fridge" and "Deal with the spare fridge" share only
    "fridge", which is one token against a five-token title — below
    `_TITLE_MATCH_MIN_SCORE`. Describing a task in your own words is the normal
    way to report finishing it, so the shortlist missing it has to widen the
    net rather than end the turn.
    """
    update_status = AsyncMock()
    reward_mock = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    query_all = AsyncMock(return_value={"results": [
        _notion_page("<page_A>", "Deal with the spare fridge in the basement"),
        _notion_page("<page_B>", "Book the dentist appointment"),
    ]})
    model = _model(json.dumps({"matched_page_id": "<page_A>", "confidence": 0.95}))

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", query_all),
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", return_value=model),
    ):
        result = await complete_module.complete_node(
            _state("finally cleaned out that garage fridge")
        )

    # The point of the fix: the model was asked, and it was shown both titles.
    model.ainvoke.assert_awaited_once()
    prompt = str(model.ainvoke.await_args.args[0][0].content)
    assert "Deal with the spare fridge in the basement" in prompt
    assert "Book the dentist appointment" in prompt

    update_status.assert_awaited_once()
    assert update_status.await_args.kwargs["page_id"] == "<page_A>"
    assert result["pending_outbound"][0]["notion_page_id"] == "<page_A>"
    assert result["pending_clarification"] is None


@pytest.mark.asyncio
async def test_widening_does_not_lower_the_write_bar() -> None:
    """A widened candidate set is a wider question, not a cheaper answer.

    0.85 clears intake's threshold and not this node's, and that stays true
    however the candidate reached the prompt.
    """
    update_status = AsyncMock()
    reward_mock = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    query_all = AsyncMock(return_value={"results": [
        _notion_page("<page_A>", "Deal with the spare fridge in the basement"),
    ]})

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", query_all),
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", return_value=_model(
            json.dumps({"matched_page_id": "<page_A>", "confidence": 0.85})
        )),
    ):
        result = await complete_module.complete_node(
            _state("finally cleaned out that garage fridge")
        )

    update_status.assert_not_awaited()
    reward_mock.assert_not_awaited()
    assert result["pending_outbound"][0]["notion_page_id"] is None


@pytest.mark.asyncio
async def test_a_null_match_over_the_widened_set_leaves_context_alone() -> None:
    """A widened null match says "I could not tell", not "that task is not done".

    "done :) feeling good" has residue the shortlist cannot place, so every open
    task becomes a candidate and the model rejects them all. Treating that as
    the #655 rejection signal would veto a live active task on the strength of
    a candidate list the message never referred to.
    """
    update_status = AsyncMock()
    reward_mock = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    query_all = AsyncMock(return_value={"results": [
        _notion_page("<page_B>", "Book the dentist appointment"),
    ]})

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", query_all),
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", return_value=_model(
            json.dumps({"matched_page_id": None, "confidence": 0.0})
        )),
    ):
        result = await complete_module.complete_node(
            _state("done :) feeling good", active_task=_live_active("<page_A>", "Fold the laundry"))
        )

    update_status.assert_awaited_once()
    assert update_status.await_args.kwargs["page_id"] == "<page_A>"
    assert result["pending_outbound"][0]["notion_page_id"] == "<page_A>"


@pytest.mark.asyncio
async def test_a_scored_null_match_still_vetoes_the_context(  # noqa: D401
) -> None:
    """#655's guard survives widening.

    "done, now I need to call mom" overlaps "Call mom" on every word, so the
    candidate is scored rather than widened, and the model's null verdict is a
    claim about that specific task: it is not finished.
    """
    update_status = AsyncMock()
    query_all = AsyncMock(return_value={"results": [_notion_page("<page_A>", "Call mom")]})

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", query_all),
        patch(
            "app.tools.rewards.maybe_reward",
            new_callable=AsyncMock,
            return_value={"text": "Nice work!", "attachment_path": None},
        ),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", return_value=_model(
            json.dumps({"matched_page_id": None, "confidence": 0.0})
        )),
    ):
        result = await complete_module.complete_node(
            _state("done, now I need to call mom", active_task=_live_active("<page_A>", "Call mom"))
        )

    update_status.assert_not_awaited()
    assert result["pending_outbound"][0]["notion_page_id"] is None


@pytest.mark.asyncio
async def test_a_bare_completion_still_reads_neither_notion_nor_the_model() -> None:
    """Widening must not turn "done!" into a Notion read plus a model call."""
    query_all = AsyncMock()
    llm_factory = MagicMock()

    with (
        patch("app.tools.notion.update_status", new_callable=AsyncMock),
        patch("app.tools.notion.query_all", query_all),
        patch(
            "app.tools.rewards.maybe_reward",
            new_callable=AsyncMock,
            return_value={"text": "Nice work!", "attachment_path": None},
        ),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", llm_factory),
    ):
        await complete_module.complete_node(
            _state("done!", active_task=_live_active("<page_A>", "Fold the laundry"))
        )

    query_all.assert_not_awaited()
    llm_factory.assert_not_called()


@pytest.mark.asyncio
async def test_a_reminder_lookup_failure_does_not_veto_the_named_task() -> None:
    """One dead source must not end the turn for the others.

    The Postgres read and the message are independent; the original early
    return let a database hiccup suppress a task the user named outright.
    """
    update_status = AsyncMock()
    reward_mock = AsyncMock(return_value={"text": "Nice work!", "attachment_path": None})
    query_all = AsyncMock(return_value={"results": [_notion_page("<page_A>", "Wash the dishes")]})

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", query_all),
        patch("app.tools.rewards.maybe_reward", reward_mock),
        patch.object(
            complete_module,
            "_load_recent_outbound_target",
            AsyncMock(side_effect=RuntimeError("postgres down")),
        ),
        patch("app.models.llm", return_value=_model(
            json.dumps({"matched_page_id": "<page_A>", "confidence": 0.95})
        )),
    ):
        result = await complete_module.complete_node(_state("done with the dishes"))

    update_status.assert_awaited_once()
    assert result["pending_outbound"][0]["notion_page_id"] == "<page_A>"


# ---------------------------------------------------------------------------
# Half two: the question survives into the answering turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unresolved_completion_records_that_it_asked() -> None:
    """The first "which task did you mean?" must leave a trace in state."""
    query_all = AsyncMock(return_value={"results": [
        _notion_page("<page_A>", "Deal with the spare fridge"),
        _notion_page("<page_B>", "Book the dentist appointment"),
    ]})

    with (
        patch("app.tools.notion.update_status", new_callable=AsyncMock),
        patch("app.tools.notion.query_all", query_all),
        patch(
            "app.tools.rewards.maybe_reward",
            new_callable=AsyncMock,
            return_value={"text": "Nice work!", "attachment_path": None},
        ),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", return_value=_model(
            json.dumps({"matched_page_id": None, "confidence": 0.0})
        )),
    ):
        result = await complete_module.complete_node(_state("knocked out that thing earlier"))

    pending = result["pending_clarification"]
    assert pending is not None
    assert pending["kind"] == "complete_target"
    assert pending["attempts"] == 1
    assert pending["candidates"], "the re-ask needs options to name"


@pytest.mark.asyncio
async def test_the_second_ask_names_options_instead_of_repeating_itself() -> None:
    """Three identical sends is what the user experienced. The re-ask differs.

    `design/adhd-priorities.md`: "If you must ask one question, offer 2-3
    constrained choices, not open-ended."
    """
    first = complete_module._clarify_completion_target("<test-peer>", attempts=0)
    second = complete_module._clarify_completion_target(
        "<test-peer>",
        attempts=1,
        candidates=(
            complete_module.DedupCandidate("<page_A>", "Deal with the spare fridge", 0.1),
            complete_module.DedupCandidate("<page_B>", "Book the dentist appointment", 0.0),
        ),
    )

    first_body = first["pending_outbound"][0]["body"]
    second_body = second["pending_outbound"][0]["body"]

    assert first_body != second_body
    assert "Deal with the spare fridge" in second_body
    assert "Book the dentist appointment" in second_body
    assert second["pending_clarification"]["attempts"] == 2


@pytest.mark.asyncio
async def test_the_agent_stops_asking_after_the_cap() -> None:
    """An unanswered question re-sent a third time costs attention and returns nothing."""
    result = complete_module._clarify_completion_target(
        "<test-peer>",
        attempts=complete_module._MAX_CLARIFICATION_ATTEMPTS,
        candidates=(
            complete_module.DedupCandidate("<page_A>", "Deal with the spare fridge", 0.1),
        ),
    )

    body = result["pending_outbound"][0]["body"]
    assert "which task" not in body.lower()
    assert result["pending_clarification"] is None, "the loop has to end, not persist"


async def _unresolved_turn(pending: dict[str, Any] | None) -> dict[str, Any]:
    """Run one COMPLETE turn that resolves nothing, carrying `pending` in."""
    query_all = AsyncMock(return_value={"results": [
        _notion_page("<page_A>", "Deal with the spare fridge"),
    ]})
    with (
        patch("app.tools.notion.update_status", new_callable=AsyncMock),
        patch("app.tools.notion.query_all", query_all),
        patch(
            "app.tools.rewards.maybe_reward",
            new_callable=AsyncMock,
            return_value={"text": "Nice work!", "attachment_path": None},
        ),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", return_value=_model(
            json.dumps({"matched_page_id": None, "confidence": 0.0})
        )),
    ):
        return await complete_module.complete_node(
            _state("the other one", pending_clarification=pending)
        )


@pytest.mark.asyncio
async def test_the_attempt_count_carries_across_turns_and_terminates() -> None:
    """The production sequence, replayed: three unresolved turns, three outcomes.

    The bug produced one body three times. The count has to survive each turn
    for the sequence to progress at all, and it has to terminate for the
    progression to mean anything.
    """
    first = await _unresolved_turn(None)
    assert first["pending_clarification"]["attempts"] == 1

    second = await _unresolved_turn(first["pending_clarification"])
    assert second["pending_clarification"]["attempts"] == 2

    third = await _unresolved_turn(second["pending_clarification"])
    assert third["pending_clarification"] is None

    bodies = [turn["pending_outbound"][0]["body"] for turn in (first, second, third)]
    assert len(set(bodies)) == 3, "the user must not get the same message three times"
    assert "which task" not in bodies[2].lower()


@pytest.mark.asyncio
async def test_an_answer_is_judged_as_an_answer_not_as_a_claim() -> None:
    """"the garden one" asserts nothing, and it is still a valid answer.

    Routing the reply back to complete_node accomplishes nothing if the
    matching prompt then judges it by the standalone rule — "match only when
    the message asserts that candidate is done". A bare noun phrase never
    asserts that, so the model rejects it every time, and the agent asks again.
    The completion claim was made on the previous turn; this message only has
    to identify which task it was about.
    """
    query_all = AsyncMock(return_value={"results": [_notion_page("<page_A>", "Water the garden")]})
    model = _model(json.dumps({"matched_page_id": "<page_A>", "confidence": 0.95}))

    with (
        patch("app.tools.notion.update_status", new_callable=AsyncMock),
        patch("app.tools.notion.query_all", query_all),
        patch(
            "app.tools.rewards.maybe_reward",
            new_callable=AsyncMock,
            return_value={"text": "Nice work!", "attachment_path": None},
        ),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", return_value=model),
    ):
        await complete_module.complete_node(
            _state("the garden one", pending_clarification=_clarification(attempts=1))
        )

    prompt = str(model.ainvoke.await_args.args[0][0].content)
    assert "was asked which one they meant" in prompt
    assert "does not need to say the task is done" in prompt
    assert "Match only when the message asserts that candidate is done" not in prompt


@pytest.mark.asyncio
async def test_an_ordinal_answer_resolves_against_the_options_that_were_named() -> None:
    """"the second one" is only an answer if the offered order is still known.

    Offering "was it A, B, or C?" and then being unable to read a positional
    reply invites the short answer and demands the long one — worse than never
    offering choices. The options lead the candidate list in the order they
    were named, and the prompt enumerates them so the ordinal has a referent.
    """
    query_all = AsyncMock(return_value={"results": [
        _notion_page("<page_B>", "Book the dentist appointment"),
        _notion_page("<page_A>", "Deal with the spare fridge"),
    ]})
    update_status = AsyncMock()
    model = _model(json.dumps({"matched_page_id": "<page_B>", "confidence": 0.95}))

    with (
        patch("app.tools.notion.update_status", update_status),
        patch("app.tools.notion.query_all", query_all),
        patch(
            "app.tools.rewards.maybe_reward",
            new_callable=AsyncMock,
            return_value={"text": "Nice work!", "attachment_path": None},
        ),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", return_value=model),
    ):
        await complete_module.complete_node(
            _state("the second one", pending_clarification=_clarification(attempts=1))
        )

    prompt = str(model.ainvoke.await_args.args[0][0].content)
    # _clarification() offers page_A first, page_B second.
    assert "1. Deal with the spare fridge" in prompt
    assert "2. Book the dentist appointment" in prompt
    assert "the second" in prompt

    update_status.assert_awaited_once()
    assert update_status.await_args.kwargs["page_id"] == "<page_B>"


@pytest.mark.asyncio
async def test_an_option_that_is_no_longer_open_cannot_come_back() -> None:
    """The offered set is a checkpoint; the open list is the authority.

    An option completed or deleted between the question and the answer must
    not be resolvable from stale state, and the surviving option keeps its
    current Notion title rather than the one stored at ask time.
    """
    query_all = AsyncMock(return_value={"results": [
        _notion_page("<page_B>", "Book the dentist appointment (rescheduled)"),
    ]})
    model = _model(json.dumps({"matched_page_id": "<page_B>", "confidence": 0.95}))

    with (
        patch("app.tools.notion.update_status", new_callable=AsyncMock),
        patch("app.tools.notion.query_all", query_all),
        patch(
            "app.tools.rewards.maybe_reward",
            new_callable=AsyncMock,
            return_value={"text": "Nice work!", "attachment_path": None},
        ),
        patch.object(
            complete_module, "_load_recent_outbound_target", AsyncMock(return_value=None)
        ),
        patch("app.models.llm", return_value=model),
    ):
        await complete_module.complete_node(
            _state("the dentist one", pending_clarification=_clarification(attempts=1))
        )

    prompt = str(model.ainvoke.await_args.args[0][0].content)
    assert "<page_A>" not in prompt, "a closed option must not be offerable again"
    assert "Book the dentist appointment (rescheduled)" in prompt
    assert "1. Book the dentist appointment (rescheduled)" in prompt


def test_offered_options_are_absent_from_a_standalone_prompt() -> None:
    """Numbering only means something after a question that named options."""
    candidates = [complete_module.DedupCandidate("<page_A>", "Call mom", 0.9)]
    standalone = complete_module._build_completion_match_prompt(
        "done with calling mom", candidates
    )
    assert "were named to the user" not in standalone


def test_a_standalone_completion_keeps_the_stricter_framing() -> None:
    """Answer mode must not leak into a first "done with the dishes".

    Without a question in front of it, the message is the only thing claiming
    anything is finished, so the assertion rule is what stops "now I need to
    call mom" from completing "Call mom".
    """
    candidates = [complete_module.DedupCandidate("<page_A>", "Call mom", 0.9)]

    standalone = complete_module._build_completion_match_prompt(
        "done, now I need to call mom", candidates
    )
    answering = complete_module._build_completion_match_prompt(
        "the mom one", candidates, answering_clarification=True
    )

    assert "asserts that candidate is done" in standalone
    assert "was asked which one they meant" not in standalone
    assert "asserts that candidate is done" not in answering

    # Both framings keep the guard that makes the 0.90 threshold meaningful.
    for prompt in (standalone, answering):
        assert "If uncertain, return no match." in prompt


# ---------------------------------------------------------------------------
# Routing: an answer to the question reaches the node that asked it
# ---------------------------------------------------------------------------


def _classifier(label: str) -> Any:
    class _Resp:
        content = label

    class _Model:
        async def ainvoke(self, _msgs: list[Any]) -> Any:
            return _Resp()

    def _factory(_tier: str, **_kwargs: Any) -> Any:
        return _Model()

    return _factory


@pytest.mark.asyncio
async def test_a_chat_shaped_answer_routes_back_to_complete() -> None:
    """"the garage one" is not a completion sentence, and it is still the answer.

    Steering to COMPLETE does not authorize anything — complete_node still has
    to match it and clear the confidence threshold.
    """
    state = _state("the garage one", pending_clarification=_clarification(attempts=1))

    with patch("app.models.llm", new=_classifier("CHAT")):
        result = await routing.classify_intent(state)

    assert result["intent"] == "COMPLETE"
    assert result["pending_clarification"] is not None


@pytest.mark.asyncio
async def test_moving_on_drops_the_clarification() -> None:
    """A user who asks for work instead of answering gets what they asked for."""
    state = _state("what should I work on next", pending_clarification=_clarification(attempts=1))

    with patch("app.models.llm", new=_classifier("GET_TASK")):
        result = await routing.classify_intent(state)

    assert result["intent"] == "GET_TASK"
    assert result["pending_clarification"] is None


@pytest.mark.asyncio
async def test_a_stale_clarification_does_not_steer_a_later_message() -> None:
    """"yeah" hours later is its own message, not an answer to a forgotten question."""
    state = _state(
        "yeah",
        pending_clarification=_clarification(attempts=1, age=timedelta(hours=3)),
    )

    with patch("app.models.llm", new=_classifier("CHAT")):
        result = await routing.classify_intent(state)

    assert result["intent"] == "CHAT"
    assert result["pending_clarification"] is None


@pytest.mark.asyncio
async def test_a_malformed_clarification_is_dropped_rather_than_trusted() -> None:
    """State that cannot be read must not steer routing."""
    state = _state("yeah", pending_clarification={"kind": "complete_target", "attempts": 1})

    with patch("app.models.llm", new=_classifier("CHAT")):
        result = await routing.classify_intent(state)

    assert result["intent"] == "CHAT"
    assert result["pending_clarification"] is None
