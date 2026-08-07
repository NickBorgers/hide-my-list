"""Unit tests for the COMPLETE node's task-reference gate.

_task_reference_tokens decides whether a completion message names a task. An
empty result means "resolve from context" and costs nothing; a non-empty result
buys a Notion read and possibly a model call. Both directions are pinned here
because the two failure modes are asymmetric:

- Wrongly deciding a message names something: one Notion read that shortlists
  nothing, then the same context-based resolution as before. Cheap.
- Wrongly deciding a message names nothing: the named task is never looked up,
  which is the production bug this path exists to fix.

Pure functions only — no mocks, no LLM, no Notion.
"""
from __future__ import annotations

import pytest

from app.graph.nodes._task_match import DedupCandidate, dice_coefficient
from app.graph.nodes.complete import (
    _ACTIVE_TASK_AGREEMENT_SCORE,
    _agrees_with_active_task,
    _build_completion_match_prompt,
    _choose_completion_target,
    _CompletionTarget,
    _task_reference_tokens,
)


def _target(source: str, page_id: str, title: str = "") -> _CompletionTarget:
    return _CompletionTarget(
        source=source,  # type: ignore[arg-type]
        page_id=page_id,
        task_title=title,
        work_type="",
        energy_required="",
        context_at=None,
    )


# ---------------------------------------------------------------------------
# The gate: messages that name nothing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "incoming",
    [
        "",
        "done",
        "done!",
        "Done!",
        "I did it",
        "yep all done",
        "finished it",
        "ok that's done",
        "done!!! finally",
        "just finished that one",
        "✅",
    ],
)
def test_messages_without_a_task_name_yield_no_tokens(incoming: str) -> None:
    """These resolve from context alone — no Notion read, no model call."""
    assert _task_reference_tokens(incoming) == set()


# ---------------------------------------------------------------------------
# The gate: messages that do name something
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("incoming", "expected_subset"),
    [
        ("done with the dishes", {"dishes"}),
        ("I finished the laundry", {"laundry"}),
        ("finally called the dentist", {"called", "dentist"}),
        ("done, the taxes are submitted", {"taxes", "submitted"}),
        ("finished writing the report", {"writing", "report"}),
    ],
)
def test_messages_naming_a_task_keep_its_words(incoming: str, expected_subset: set[str]) -> None:
    assert expected_subset <= _task_reference_tokens(incoming)


def test_completion_words_never_strip_real_title_words() -> None:
    """Words that are completion-flavored but also real task titles survive.

    Adding any of these to the stopword set would silently re-open the bug for
    every task whose title is built from them.
    """
    residue = _task_reference_tokens("done: call mom, pay rent, clean the sink, sort mail")
    assert {"call", "mom", "pay", "rent", "clean", "sink", "sort", "mail"} <= residue


def test_filler_only_message_shortlists_nothing_downstream() -> None:
    """A false 'names something' costs a Notion read and stops at the shortlist.

    "knocked that out" leaves {knocked}, which overlaps no title, so the
    shortlist is empty and no model call happens.
    """
    residue = _task_reference_tokens("knocked that out")
    assert residue == {"knocked"}
    assert dice_coefficient(residue, {"take", "trash"}) == 0.0


# ---------------------------------------------------------------------------
# Active-task agreement short-circuit
# ---------------------------------------------------------------------------

def test_naming_the_active_task_agrees() -> None:
    active = _target("active_task", "<page_A>", "Fold the laundry")
    assert _agrees_with_active_task(_task_reference_tokens("done with the laundry"), active)


def test_naming_a_different_task_does_not_agree() -> None:
    active = _target("active_task", "<page_A>", "Fold the laundry")
    assert not _agrees_with_active_task(_task_reference_tokens("done with the dishes"), active)


def test_agreement_requires_an_active_task() -> None:
    assert not _agrees_with_active_task({"laundry"}, None)
    assert not _agrees_with_active_task(set(), _target("active_task", "<page_A>", "Anything"))


def test_agreement_threshold_is_the_dice_score() -> None:
    active = _target("active_task", "<page_A>", "Fold the laundry before bed")
    # {laundry} against {fold, laundry, before, bed} scores exactly 0.4.
    score = dice_coefficient({"laundry"}, {"fold", "laundry", "before", "bed"})
    assert score == pytest.approx(_ACTIVE_TASK_AGREEMENT_SCORE)
    assert _agrees_with_active_task({"laundry"}, active)


# ---------------------------------------------------------------------------
# No lexical shortcut past the model
# ---------------------------------------------------------------------------

def test_quoting_a_whole_title_is_not_by_itself_a_completion() -> None:
    """Containing a title's every word does not mean the message says it is done.

    "done, now I need to call mom" contains all of "Call mom" while asserting
    the opposite. The shortlist surfaces the candidate either way; only the
    model reading the whole sentence can tell the two apart, so the matcher
    keeps no exact-title fast path around it.
    """
    residue = _task_reference_tokens("done, now I need to call mom")
    title_tokens = _task_reference_tokens("Call mom")
    assert title_tokens <= residue
    assert dice_coefficient(residue, title_tokens) >= 0.30


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------

def test_named_task_outranks_a_different_active_task() -> None:
    """Without this, naming task B while task A is active completes A."""
    active = _target("active_task", "<page_A>")
    title = _target("title_match", "<page_B>")
    chosen = _choose_completion_target(
        active_target=active, recent_target=None, title_target=title
    )
    assert chosen is not None
    assert chosen.page_id == "<page_B>"


def test_named_task_outranks_recent_outbound() -> None:
    recent = _target("recent_outbound", "<page_A>")
    title = _target("title_match", "<page_B>")
    chosen = _choose_completion_target(
        active_target=None, recent_target=recent, title_target=title
    )
    assert chosen is not None
    assert chosen.source == "title_match"


def test_same_page_prefers_the_active_task_for_its_reward_metadata() -> None:
    """active_task is the only source carrying work_type / energy_required."""
    active = _CompletionTarget(
        source="active_task",
        page_id="<page_A>",
        task_title="Fold the laundry",
        work_type="Physical",
        energy_required="Low",
        context_at=None,
    )
    title = _target("title_match", "<page_A>", "Fold the laundry")
    chosen = _choose_completion_target(
        active_target=active, recent_target=None, title_target=title
    )
    assert chosen is not None
    assert chosen.source == "active_task"
    assert chosen.work_type == "Physical"


def test_no_title_match_leaves_existing_precedence_untouched() -> None:
    recent = _target("recent_outbound", "<page_A>")
    active = _target("active_task", "<page_B>")
    chosen = _choose_completion_target(
        active_target=active, recent_target=recent, title_target=None
    )
    assert chosen is not None
    assert chosen.page_id == "<page_A>"


# ---------------------------------------------------------------------------
# Notion write policy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("source", "expected"),
    [("active_task", True), ("title_match", True), ("recent_outbound", False)],
)
def test_needs_notion_write_is_derived_from_the_source(source: str, expected: bool) -> None:
    """Reminder pages are already Completed at delivery; every other source writes."""
    assert _target(source, "<page_A>").needs_notion_write is expected


# ---------------------------------------------------------------------------
# Prompt contract
# ---------------------------------------------------------------------------

def test_prompt_asks_whether_the_task_is_already_finished() -> None:
    """The question is 'is this done?', not intake's 'is this the same task?'.

    A same-task prompt matches "done, now I need to call mom" against an open
    "Call mom" and completes a task the user just said they still have to do.
    """
    prompt = _build_completion_match_prompt(
        "done, now I need to call mom",
        [DedupCandidate(page_id="<page_A>", title="Call mom", score=0.8)],
    )
    assert "ALREADY FINISHED" in prompt
    assert "still intends to do" in prompt
    assert "<page_A>" in prompt
    assert '{"matched_page_id"' in prompt


def test_prompt_uses_no_bracketed_placeholder_slots() -> None:
    """Bracketed slots read as an instruction to paraphrase (see _task_token)."""
    prompt = _build_completion_match_prompt(
        "done with the dishes",
        [DedupCandidate(page_id="<page_A>", title="Wash the dishes", score=0.9)],
    )
    assert "[task]" not in prompt
    assert "[title]" not in prompt
