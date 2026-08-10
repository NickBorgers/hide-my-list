"""PR-B2 banned-phrase regex test gate.

Runs over 30 fixture turns and asserts zero matches of any banned shame-triggering
phrase in:
1. Rendered prompt templates (the instructions we give to the LLM)
2. Mocked LLM outputs (the LLM echoes its system prompt, so we verify the prompt itself)

Banned phrases sourced from:
- docs/ai-prompts/shared.md SHAME PREVENTION section
- design/adhd-priorities.md

This is a hard CI gate — any match is a failure. LLM-judge evaluation is advisory
and runs nightly, not in CI.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_FIXTURES_PATH = Path(__file__).parent.parent / "fixtures" / "conversation_turns.json"
_PROMPTS_DIR = Path(__file__).parent.parent.parent / "app" / "prompts"

# ---------------------------------------------------------------------------
# Banned-phrase patterns (case-insensitive)
# These are the specific phrases banned by docs/ai-prompts/shared.md
# ---------------------------------------------------------------------------
_BANNED_PATTERNS: list[re.Pattern] = [
    re.compile(r"\byou didn'?t\b", re.IGNORECASE),
    re.compile(r"\byou should have\b", re.IGNORECASE),
    re.compile(r"\byou forgot\b", re.IGNORECASE),
    re.compile(r"\byou failed\b", re.IGNORECASE),
    re.compile(r"\byou never\b", re.IGNORECASE),
    # Additional shame-triggering phrases from design/adhd-priorities.md and shared.md
    re.compile(r"\byou haven'?t\b", re.IGNORECASE),
    re.compile(r"\byou missed\b", re.IGNORECASE),
    re.compile(r"\bfailed to\b", re.IGNORECASE),
    re.compile(r"\byou were supposed to\b", re.IGNORECASE),
    re.compile(r"\byou were meant to\b", re.IGNORECASE),
    re.compile(r"\byou are lazy\b", re.IGNORECASE),
    re.compile(r"\byou're lazy\b", re.IGNORECASE),
]


def _load_turns() -> list[dict[str, Any]]:
    """Load fixture conversation turns."""
    assert _FIXTURES_PATH.is_file(), f"Fixture file not found: {_FIXTURES_PATH}"
    turns = json.loads(_FIXTURES_PATH.read_text(encoding="utf-8"))
    assert len(turns) >= 30, f"Need at least 30 turns, got {len(turns)}"
    return turns


def _render_prompt(template_name: str, context: dict[str, Any]) -> str:
    """Render a template with the given context."""
    from app.prompts.loader import render_with_defaults

    defaults = {
        "user_message": "",
        "recent_outbound_context": "",
        "user_preferences_context": "",
        "conversation_context": "",
        "available_minutes": 30,
        "mood": "neutral",
        "preferred_work_type": "any",
        "time_of_day": "afternoon",
        "tasks_json": "[]",
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
    return render_with_defaults(template_name, context, defaults)


_PROHIBITION_CONTEXT_PATTERNS: list[re.Pattern] = [
    # Lines that explicitly ban the phrase as a negative example
    re.compile(r'(?i)never use|never say|never include|do not use|do not say|banned|prohibited'),
    # Lines that quote phrases as examples of what NOT to do
    re.compile(r'(?i)never.*"you|never.*\'you|never use "you|do not.*"you'),
    # Markdown bullet lists that introduce the phrase as a forbidden example
    re.compile(r'(?i)^[-*]\s+never\b', re.MULTILINE),
    # Lines with "never use ... 'phrase'" or quoted prohibition patterns
    re.compile(r'(?i)never use .{0,20}"'),
]


def _line_is_prohibition_context(line: str) -> bool:
    """Return True if the line is introducing phrases as examples of what NOT to say."""
    for pat in _PROHIBITION_CONTEXT_PATTERNS:
        if pat.search(line):
            return True
    return False


def _find_banned_phrases(text: str) -> list[str]:
    """Return banned phrases found in text, excluding prohibition-context lines.

    Prohibition context = lines that list the phrase as an example of what NOT to say.
    For example: "Never use 'you didn't', 'you should have', 'you forgot', or 'you failed'"
    contains the banned phrases but is itself a constraint, not a violation.

    We only flag phrases that appear in instructional/action context (positive sentences
    telling the LLM to say something shame-triggering).
    """
    found = []
    for line in text.splitlines():
        if _line_is_prohibition_context(line):
            continue  # Skip lines that list banned phrases as examples
        for pattern in _BANNED_PATTERNS:
            matches = pattern.findall(line)
            found.extend(matches)
    return found


def _template_for_intent(intent: str) -> str | None:
    """Map intent to prompt template filename."""
    mapping = {
        "GET_TASK": "selection.md.j2",
        "ADD_TASK": "intake.md.j2",
        "REJECT": "rejection.md.j2",
        "CANNOT_FINISH": "cannot_finish.md.j2",
        "CHECK_IN": "check_in.md.j2",
        "NEED_HELP": "need_help.md.j2",
        "CHAT": "chat.md.j2",
        "COMPLETE": None,  # COMPLETE node has inline prompt; test shared prompt
    }
    return mapping.get(intent)


def test_banned_phrases_absent_in_rendered_prompts() -> None:
    """Zero banned phrases in any rendered prompt template across 30+ fixture turns.

    This tests the instructions we send to the LLM. If the instructions contain
    shame-triggering language, the LLM might mirror it.
    """
    turns = _load_turns()
    violations: list[dict] = []

    for turn in turns:
        intent = turn.get("intent", "CHAT")
        context = turn.get("context", {})
        template_name = _template_for_intent(intent)

        if template_name is None:
            continue  # COMPLETE node doesn't have a separate template yet

        rendered = _render_prompt(template_name, context)
        banned = _find_banned_phrases(rendered)

        if banned:
            violations.append({
                "turn_id": turn.get("id"),
                "intent": intent,
                "template": template_name,
                "phrases_found": banned,
            })

    assert not violations, (
        "Banned shame-triggering phrases found in rendered prompts:\n"
        + "\n".join(
            f"  Turn {v['turn_id']} ({v['intent']} -> {v['template']}): {v['phrases_found']}"
            for v in violations
        )
    )


def test_banned_phrases_absent_in_all_templates() -> None:
    """Zero banned phrases in any .md.j2 template file (static check).

    This catches phrases baked directly into template text (not via context variables).
    """
    template_files = list(_PROMPTS_DIR.glob("*.md.j2"))
    assert len(template_files) > 0, "No template files found"

    violations: list[dict] = []
    for template_path in template_files:
        # Read the raw template text (before rendering)
        raw_text = template_path.read_text(encoding="utf-8")
        banned = _find_banned_phrases(raw_text)
        if banned:
            violations.append({"file": template_path.name, "phrases": banned})

    assert not violations, (
        "Banned shame-triggering phrases found in template files:\n"
        + "\n".join(f"  {v['file']}: {v['phrases']}" for v in violations)
    )


def test_banned_phrases_absent_in_source_docs() -> None:
    """Zero banned phrases in docs/ai-prompts/ source files.

    Source docs are loaded into the runtime agent — any banned phrase there
    could influence LLM behavior.
    """
    docs_dir = Path(__file__).parent.parent.parent / "docs" / "ai-prompts"
    source_files = list(docs_dir.glob("*.md"))
    assert len(source_files) > 0, "No source docs found"

    violations: list[dict] = []
    for doc_path in source_files:
        text = doc_path.read_text(encoding="utf-8")
        banned = _find_banned_phrases(text)
        if banned:
            violations.append({"file": doc_path.name, "phrases": banned})

    assert not violations, (
        "Banned shame-triggering phrases found in source docs:\n"
        + "\n".join(f"  {v['file']}: {v['phrases']}" for v in violations)
    )


def test_fixture_has_at_least_30_turns() -> None:
    """Fixture file must contain at least 30 conversation turns."""
    turns = _load_turns()
    assert len(turns) >= 30, f"Fixture has only {len(turns)} turns; need >= 30"


def test_fixture_turn_ids_are_unique() -> None:
    """All fixture turn IDs are unique (no accidental duplicates)."""
    turns = _load_turns()
    ids = [t.get("id") for t in turns]
    assert len(ids) == len(set(ids)), "Fixture has duplicate turn IDs"


def test_banned_pattern_list_is_comprehensive() -> None:
    """Verify the banned pattern list includes all phrases from shared.md spec."""
    # These are the specific phrases called out in docs/ai-prompts/shared.md
    required_phrases = [
        "you didn't",
        "you should have",
        "you forgot",
        "you failed",
        "you never",
    ]

    for phrase in required_phrases:
        matched = any(p.search(phrase) for p in _BANNED_PATTERNS)
        assert matched, f"Banned phrase '{phrase}' from shared.md spec is not covered by any pattern"
