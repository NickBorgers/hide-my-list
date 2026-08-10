"""Structural lint: review-fixer.yml pre-commit lint gate.

Asserts that review-fixer.yml contains a full-tree ruff lint step that runs
before the commit step, and that the host-side commit step activates the
pre-commit hook via core.hooksPath.

Bug class prevention: closes the gap where the fixer pipeline's host-side
git commit skipped the pre-commit hook entirely (no GIT_CONFIG_COUNT set)
and where ruff only checked staged files (hook) rather than the full tree
being pushed. See PR #594 — a pre-existing UP037 violation introduced by
PR #589 propagated through the fixer undetected.
"""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
_FIXER_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "review-fixer.yml"


def _workflow_text() -> str:
    return _FIXER_WORKFLOW.read_text(encoding="utf-8")


def test_lint_gate_step_present() -> None:
    """review-fixer.yml must contain the full-tree ruff lint gate step."""
    text = _workflow_text()
    assert "Ruff lint gate" in text, (
        "Expected a 'Ruff lint gate' step in review-fixer.yml. "
        "Without it, the fixer can push commits that fail ruff on files it "
        "didn't modify (pre-existing violations from main). See PR #594."
    )


def test_lint_gate_runs_ruff_on_full_tree() -> None:
    """The lint gate step must invoke ruff check on app/ tests/ scripts/."""
    text = _workflow_text()
    assert "ruff check app/ tests/ scripts/" in text, (
        "Expected 'ruff check app/ tests/ scripts/' in the lint gate step. "
        "Staged-files-only coverage misses pre-existing violations."
    )


def test_lint_gate_precedes_commit_step() -> None:
    """The ruff lint gate step must appear before 'Commit fixer working-tree changes'."""
    text = _workflow_text()
    lint_pos = text.find("Ruff lint gate")
    commit_pos = text.find("Commit fixer working-tree changes")
    assert lint_pos != -1, "Ruff lint gate step not found in review-fixer.yml"
    assert commit_pos != -1, "'Commit fixer working-tree changes' step not found"
    assert lint_pos < commit_pos, (
        "Ruff lint gate step must appear before 'Commit fixer working-tree changes'. "
        "Lint that runs after the commit can't block the push."
    )


def test_install_step_precedes_lint_gate() -> None:
    """An 'Install pre-commit hook tooling' step must precede the ruff lint gate.

    The host commit step sets core.hooksPath=.githooks, which activates
    .githooks/pre-commit on every fixer commit. The hook calls
    scripts/run-required-checks.sh, which hard-fails when yamllint / ruff
    are missing — and GH Actions hosts don't ship them by default.
    Without the install step, the fixer aborts every commit on a workflow PR
    with "required command 'yamllint' is not installed".

    pytest is deliberately excluded: the unit suite needs the project's
    runtime dependencies importable, which the host job does not install.
    The commit step sets HML_SKIP_HOOK_PYTEST=1 instead — see
    tests/unit/test_fixer_pytest_hook_skip.py.
    """
    text = _workflow_text()
    install_pos = text.find("Install pre-commit hook tooling")
    lint_pos = text.find("Ruff lint gate")
    commit_pos = text.find("Commit fixer working-tree changes")
    assert install_pos != -1, (
        "Expected an 'Install pre-commit hook tooling' step in review-fixer.yml. "
        "The host commit's pre-commit hook needs yamllint / ruff on PATH; "
        "GH Actions runners don't provide them."
    )
    assert install_pos < lint_pos < commit_pos, (
        "Install step must run before the ruff lint gate (which uses ruff) and "
        "before the commit step (whose hook uses yamllint / ruff)."
    )
    install_block_end = text.find("\n      - name:", install_pos + 1)
    install_block = text[install_pos:install_block_end] if install_block_end != -1 else text[install_pos:]
    for tool in ("yamllint", "ruff"):
        assert tool in install_block, (
            f"Install step must install '{tool}'. The pre-commit hook requires it "
            f"whenever the fixer stages a file in its scope."
        )


def test_commit_step_activates_pre_commit_hook() -> None:
    """The commit step must set GIT_CONFIG_COUNT so core.hooksPath is active."""
    text = _workflow_text()
    # Find the commit step and check that GIT_CONFIG env vars are set there.
    # We look for GIT_CONFIG_COUNT within a reasonable proximity to the commit step.
    commit_pos = text.find("Commit fixer working-tree changes")
    assert commit_pos != -1, "'Commit fixer working-tree changes' step not found"

    # Slice from the commit step to the next step header ("      - name:")
    # to avoid picking up GIT_CONFIG from the container run steps.
    commit_block_start = commit_pos
    # Find the next step definition after the commit step header
    next_step_pos = text.find("\n      - name:", commit_pos + 1)
    commit_block = text[commit_block_start:next_step_pos] if next_step_pos != -1 else text[commit_block_start:]

    assert "GIT_CONFIG_COUNT" in commit_block, (
        "Expected GIT_CONFIG_COUNT in the 'Commit fixer working-tree changes' step env. "
        "Without it, core.hooksPath is not set on the host runner and the pre-commit "
        "hook (.githooks/pre-commit) is skipped for the fixer's staged changes."
    )
    assert "core.hooksPath" in commit_block, (
        "Expected core.hooksPath in the 'Commit fixer working-tree changes' step env. "
        "The pre-commit hook lives in .githooks/, not .git/hooks/, so git won't find "
        "it without an explicit hooksPath override."
    )
