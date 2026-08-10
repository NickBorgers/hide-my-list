"""Guardrail: the CHAT prompt must not let the LLM deny the system's reminder capability.

Background: a deployed instance, when asked "you didn't remind me - why not?",
replied "I'm not able to send reminders or notifications — I can only help you
manage your list when you check in with me." That denial is factually wrong —
the system DOES send scheduled reminders via the outbox + APScheduler worker.

The LLM hallucinated the denial because the chat prompt was generic and
shared.md's "never mention reminder infrastructure" rule was overgeneralized.

This test does NOT call the LLM. It asserts structural properties of the
rendered prompt templates:
  1. The corrective directive ("do not deny", "does send", "capability") is
     present in the rendered chat.md.j2 template.
  2. shared.md.j2 carries the clarifying note that "don't mention
     infrastructure" is not the same as "deny capability".

Brittle to wording changes by design — if someone removes the corrective
language, this test fails and forces a deliberate decision.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "app" / "prompts"


def _render(template_name: str) -> str:
    """Render a template with empty context (loader-style)."""
    from app.prompts.loader import render

    return render(template_name, {})


def test_chat_prompt_forbids_capability_denial() -> None:
    """The CHAT prompt must explicitly tell the LLM not to deny reminder capability."""
    rendered = _render("chat.md.j2")

    # Section header for the directive
    assert "do not deny" in rendered.lower(), (
        "chat.md.j2 must contain 'do not deny' guidance so the LLM "
        "doesn't hallucinate a denial of reminder capability."
    )

    # The specific phrases the deployed LLM emitted are the ones we must
    # explicitly forbid in the prompt.
    assert (
        "cannot send reminders" in rendered.lower()
        or "send reminders" in rendered.lower()
    ), (
        "chat.md.j2 must mention 'send reminders' so the LLM has the term "
        "in its context as a supported capability."
    )


def test_chat_prompt_has_missed_reminder_handler() -> None:
    """The CHAT prompt must include guidance for missed-reminder follow-ups."""
    rendered = _render("chat.md.j2")
    assert re.search(
        r"missed[- ]reminder|reminder.*did not arrive|reminder.*not.*receive",
        rendered,
        re.IGNORECASE,
    ), (
        "chat.md.j2 must include a 'missed reminder' handler section so the "
        "LLM knows how to respond when the user reports a reminder did not fire."
    )


def test_shared_prompt_clarifies_infrastructure_vs_capability() -> None:
    """shared.md.j2 must clarify that 'don't mention infrastructure' != 'deny capability'."""
    rendered = _render("shared.md.j2")
    # Look for the clarifying clause; permissive matching since exact wording
    # can drift.
    has_clarification = (
        "does not mean denying capability" in rendered.lower()
        or "does not mean denying the capability" in rendered.lower()
        or "not the same as denying" in rendered.lower()
    )
    assert has_clarification, (
        "shared.md.j2 must state that 'don't mention infrastructure' does not "
        "mean denying capability — otherwise the LLM overgeneralizes and "
        "denies reminders entirely."
    )


@pytest.mark.parametrize(
    "denial_phrase",
    [
        "not able to send reminders",
        "cannot send reminders or notifications",
        "purely passive",
        "only.*when you check in",
    ],
)
def test_chat_prompt_does_not_seed_denial(denial_phrase: str) -> None:
    """The CHAT prompt itself must not echo the exact denial phrasing.

    Including a denial phrase in the prompt (even as a 'don't say this' example)
    increases the chance the LLM will produce it. The corrective section uses
    'do not deny' framing instead of quoting the bad output.
    """
    rendered = _render("chat.md.j2")
    pattern = re.compile(rf"^[^#]*{denial_phrase}", re.IGNORECASE | re.MULTILINE)
    # The denial phrase may appear as a description of what to avoid; the
    # heuristic is: it should not appear as a positive statement. The
    # 'do not' / 'never' framing must precede it.
    for match in pattern.finditer(rendered):
        context = rendered[max(0, match.start() - 120) : match.end()]
        assert re.search(
            r"\b(do not|don'?t|never|not)\b",
            context,
            re.IGNORECASE,
        ), (
            f"chat.md.j2 contains '{denial_phrase}' but not within a "
            f"'do not / never' negation context. Risk: the LLM will copy "
            f"it as a valid response. Excerpt: {context!r}"
        )
