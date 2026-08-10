"""Guard the `{task}` prompt convention.

A bracketed placeholder like `[task]` reads to a model as "describe something
here", so it produces bodies such as "how about this focus task?" that name no
task. The convention is the literal token `{task}`, which the application
substitutes with the exact stored title (app/graph/nodes/_task_token.py).

This test exists because the same bug shipped twice — once in the rejection
path, once in selection — from prompts that each independently used `[task]`.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = REPO_ROOT / "app" / "prompts"
NODE_DIR = REPO_ROOT / "app" / "graph" / "nodes"

# Bracketed slots that stand in for a task the user must be able to act on.
# Prose slots the model should author itself ([time], [mood], [urgency level])
# are deliberately not matched.
_BANNED = re.compile(r"\[(?:shorter\s+|alternative\s+|selected\s+|sub-)?tasks?\]", re.IGNORECASE)

_GUIDANCE = (
    "Use the literal token {task} instead. The application substitutes the exact "
    "title via app/graph/nodes/_task_token.render_task_token; a bracketed slot "
    "invites the model to paraphrase and produces an unactionable message."
)


def _prompt_templates() -> list[Path]:
    return sorted(PROMPT_DIR.glob("*.md.j2"))


def test_prompt_templates_exist() -> None:
    """Fail loudly if the glob stops matching — an empty scan proves nothing."""
    assert _prompt_templates(), f"no prompt templates found under {PROMPT_DIR}"


@pytest.mark.parametrize("template", _prompt_templates(), ids=lambda p: p.name)
def test_template_has_no_bracketed_task_placeholder(template: Path) -> None:
    """Prompt templates name tasks via {task}, never a bracketed slot."""
    hits = _BANNED.findall(template.read_text(encoding="utf-8"))
    assert not hits, f"{template.relative_to(REPO_ROOT)} contains {hits}. {_GUIDANCE}"


# _task_token.py documents the banned pattern in order to explain the rule.
_EXEMPT_MODULES = {"_task_token.py"}


@pytest.mark.parametrize(
    "module",
    [p for p in sorted(NODE_DIR.glob("*.py")) if p.name not in _EXEMPT_MODULES],
    ids=lambda p: p.name,
)
def test_node_module_has_no_bracketed_task_placeholder(module: Path) -> None:
    """Inline prompt text in node modules follows the same convention.

    Node prompts live in app/prompts/*.md.j2. Any prompt text that reappears in
    a node module is a second copy that drifts silently from the template, so
    it is held to the same rule.
    """
    hits = _BANNED.findall(module.read_text(encoding="utf-8"))
    assert not hits, f"{module.relative_to(REPO_ROOT)} contains {hits}. {_GUIDANCE}"
