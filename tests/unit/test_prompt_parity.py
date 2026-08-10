"""PR-B2 section-anchor parity test.

Extracts ## headings from each docs/ai-prompts/*.md source file and asserts
that every heading appears in the rendered app/prompts/*.md.j2 template.

This is NOT byte-match comparison — it validates structural parity (every named
section from the source spec is present in the rendered template). Minor prose
differences are allowed; missing sections are failures.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_DOCS_DIR = Path(__file__).parent.parent.parent / "docs" / "ai-prompts"
_PROMPTS_DIR = Path(__file__).parent.parent.parent / "app" / "prompts"

# Mapping from source doc to rendered template
# Key: source file in docs/ai-prompts/
# Value: template file in app/prompts/
_PARITY_MAP: dict[str, str] = {
    "shared.md": "shared.md.j2",
    "selection.md": "selection.md.j2",
    "intake.md": "intake.md.j2",
    "rejection.md": "rejection.md.j2",
    "cannot-finish.md": "cannot_finish.md.j2",
    "check-in.md": "check_in.md.j2",
    "breakdown.md": "need_help.md.j2",
}

# Some source headings are mermaid-diagram labels or sub-headings that are
# intentionally not in the rendered template (they are visual aids in the docs,
# not prose sections). Allow-list them to avoid false negatives.
_EXCLUDED_HEADINGS: set[str] = {
    # Mermaid node labels and diagram titles
    "## Overview",
    "## Module 1: Intent Detection",
    "## Module 2: Task Intake",
    "## Module 3: Task Selection",
    "## Module 4: Rejection Handling",
    "## Module 5: Cannot Finish Handling",
    "## Module 6: Check-In Handling",
    "## Module 7: Breakdown Assistance (NEED_HELP)",
    # High-level sections that are meta-commentary in docs
    "## Prompt Architecture",
    "## Visible Output Boundary",
    "## Structured Output Handling",
    "## Error Handling",
    "## Prompt Versioning",
    "## Conversation State Management",
    "## Example Complete Flow",
    "## User Preferences Context",
}


def _extract_headings(text: str) -> list[str]:
    """Extract all ## headings from a markdown document."""
    headings = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            headings.append(stripped)
    return headings


def _render_template_with_empty_context(template_name: str) -> str:
    """Render a Jinja2 template with an empty context (for parity checks).

    Uses defaults for all template variables so StrictUndefined doesn't fire.
    """
    from app.prompts.loader import render

    # Provide all known template variables with empty/default values
    context = {
        "user_message": "",
        "recent_outbound_context": "",
        "user_preferences_context": "",
        "available_minutes": 30,
        "mood": "neutral",
        "preferred_work_type": "any",
        "time_of_day": "afternoon",
        "tasks_json": "[]",
        "prior_conversation": "user: placeholder prior turn",
        "conversation_context": "",
        "task_title": "placeholder task",
        "rejection_reason": "",
        "remaining_tasks_json": "[]",
        "user_timezone": "America/Chicago",
        "current_time": "2026-01-01T12:00:00-06:00",
        "time_estimate": 30,
        "elapsed_minutes": 30,
        "check_in_count": 0,
        "inline_steps": "1. Step one\n2. Step two",
        "conversation_history": "",
        "clarification_count": 0,
    }
    return render(template_name, context)


@pytest.mark.parametrize("source_name,template_name", list(_PARITY_MAP.items()))
def test_section_anchor_parity(source_name: str, template_name: str) -> None:
    """Every ## heading in the source doc must appear in the rendered template.

    This ensures that porting a spec doc to a Jinja2 template does not silently
    drop any named section. The test is anchor-based (heading text), not byte-match.
    """
    source_path = _DOCS_DIR / source_name
    template_path = _PROMPTS_DIR / template_name

    assert source_path.is_file(), f"Source doc not found: {source_path}"
    assert template_path.is_file(), f"Template not found: {template_path}"

    source_text = source_path.read_text(encoding="utf-8")
    rendered_text = _render_template_with_empty_context(template_name)

    source_headings = [
        h for h in _extract_headings(source_text)
        if h not in _EXCLUDED_HEADINGS
    ]

    missing = []
    for heading in source_headings:
        # Search for the heading text (without ## prefix) in the rendered output
        heading_text = heading.lstrip("# ").strip()
        if heading_text and heading_text not in rendered_text:
            missing.append(heading)

    assert not missing, (
        f"Template '{template_name}' is missing sections from '{source_name}':\n"
        + "\n".join(f"  {h}" for h in missing)
    )


def test_all_source_docs_covered() -> None:
    """All ai-prompts docs with a known mapping are in the parity map."""
    # Verify the parity map covers all docs we care about
    # (Some docs like shared.md have only base sections covered by shared.md.j2)
    for source_name in _PARITY_MAP:
        assert (_DOCS_DIR / source_name).is_file(), (
            f"Parity map references non-existent source: {source_name}"
        )
    for template_name in _PARITY_MAP.values():
        assert (_PROMPTS_DIR / template_name).is_file(), (
            f"Parity map references non-existent template: {template_name}"
        )


def test_templates_render_without_error() -> None:
    """All templates must render without Jinja2 errors when given empty context."""
    for template_name in _PARITY_MAP.values():
        rendered = _render_template_with_empty_context(template_name)
        assert len(rendered) > 10, f"Template {template_name} rendered as near-empty"


def test_shared_template_prior_conversation_contract() -> None:
    """shared.md.j2 must surface the prior_conversation block and must not reference
    the obsolete RECENT_OUTBOUND_CONTEXT anchor.

    Regression guard for #574: the template replaced RECENT_OUTBOUND_CONTEXT with
    a windowed Prior conversation block. This test fails if the template is rolled
    back to the old anchor or the new block is removed.
    """
    from app.prompts.loader import render

    sentinel = "test-prior-turn-sentinel"
    rendered = render("shared.md.j2", {"prior_conversation": sentinel})

    assert "Prior conversation" in rendered, (
        "shared.md.j2 must render a 'Prior conversation' heading/label in the intent"
        " detection prompt"
    )
    assert sentinel in rendered, (
        "prior_conversation value must appear in the rendered shared.md.j2 output"
    )
    assert "RECENT_OUTBOUND_CONTEXT" not in rendered, (
        "obsolete RECENT_OUTBOUND_CONTEXT anchor must not appear in shared.md.j2"
    )
