"""Structural guard for the cannot_finish prompt output schema.

Locks the output-field contract introduced by the cannot-finish echo fix:
- progress_question and next_sub_task_message are the user-facing output fields
- user_message must not appear as a JSON output key in the schema
- the no-echo instruction must be present in the template

Regression guard for bug 0647: models fill a field named user_message with an
echo of the inbound message. Removing the field from the output schema eliminates
the echo risk at the highest shame-vulnerability moment in the flow.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = REPO_ROOT / "app" / "prompts" / "cannot_finish.md.j2"


def _raw() -> str:
    assert TEMPLATE_PATH.is_file(), f"Template not found: {TEMPLATE_PATH}"
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def test_progress_question_in_output_schema() -> None:
    """Output schema must include progress_question as a user-facing field."""
    assert '"progress_question"' in _raw(), (
        'cannot_finish.md.j2 output schema must contain \'"progress_question"\''
    )


def test_next_sub_task_message_in_output_schema() -> None:
    """Output schema must include next_sub_task_message as a user-facing field."""
    assert '"next_sub_task_message"' in _raw(), (
        'cannot_finish.md.j2 output schema must contain \'"next_sub_task_message"\''
    )


def test_user_message_not_a_json_output_key() -> None:
    """user_message must not appear as a JSON output key in the schema.

    Models fill a field named user_message with an echo of the inbound text,
    causing a shame-risk echo at the highest-vulnerability moment in the flow.
    The input variable reference (USER MESSAGE: ...) is fine; the banned pattern
    is the JSON key form '"user_message"' appearing in the output schema.
    """
    assert '"user_message"' not in _raw(), (
        'cannot_finish.md.j2 must not contain \'"user_message"\' as a JSON output '
        "key — models echo inbound text into a field with that name"
    )


def test_no_echo_instruction_present() -> None:
    """Template must instruct the model never to repeat the user's message back."""
    raw = _raw()
    assert "never repeat the user" in raw.lower(), (
        "cannot_finish.md.j2 must contain a no-echo instruction "
        "(e.g. 'Never repeat the user's message back in any field')"
    )
