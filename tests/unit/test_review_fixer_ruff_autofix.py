"""Structural lint: review-fixer.yml ruff auto-fix pass.

Asserts that the fixer job repairs its own auto-fixable lint violations
before the terminal full-tree ruff gate runs, and that the auto-fix is
scoped to the files the fixer touched.

Bug class prevention: `fixer.md` forbids the fixer from running ruff, so
the fixer cannot see its own mechanical lint slips. The terminal gate is
all-or-nothing — it refuses the commit and the entire cycle's work is
discarded, unpushed. Observed on PR #626: a single unused-import F401 in
a test file the fixer wrote cost a full reviewer fan-out plus the fixer
run, and a human had to hand-apply changes ruff repairs deterministically.

The scoping half matters as much as the auto-fix half. Running `--fix`
across the whole tree would let the fixer silently launder a lint
regression that landed on main, which the terminal gate exists to surface
and route to its own PR.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
_FIXER_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "review-fixer.yml"

_AUTOFIX_STEP = "Ruff auto-fix (fixer-touched files)"
_GATE_STEP = "Ruff lint gate (full tree, pre-commit)"
_COMMIT_STEP = "Commit fixer working-tree changes"


def _workflow_text() -> str:
    return _FIXER_WORKFLOW.read_text(encoding="utf-8")


def _step_index(text: str, step_name: str) -> int:
    idx = text.find(f"- name: {step_name}")
    assert idx != -1, (
        f"Expected a '{step_name}' step in review-fixer.yml but found none."
    )
    return idx


def test_autofix_step_present() -> None:
    """The fixer job must include the scoped ruff auto-fix pass."""
    text = _workflow_text()
    assert _AUTOFIX_STEP in text, (
        f"Expected a '{_AUTOFIX_STEP}' step in review-fixer.yml. Without it, "
        "an auto-fixable violation in fixer-authored code discards the whole "
        "cycle at the terminal lint gate. See PR #626."
    )


def test_autofix_runs_before_lint_gate_and_commit() -> None:
    """Auto-fix is pointless unless it precedes the gate it exists to satisfy."""
    text = _workflow_text()
    autofix_at = _step_index(text, _AUTOFIX_STEP)
    gate_at = _step_index(text, _GATE_STEP)
    commit_at = _step_index(text, _COMMIT_STEP)

    assert autofix_at < gate_at, (
        "The ruff auto-fix pass must run BEFORE the full-tree lint gate. "
        "Ordered after it, the gate still fails on violations that were "
        "repairable and the cycle is still discarded."
    )
    assert gate_at < commit_at, (
        "The full-tree lint gate must remain BEFORE the commit step so no "
        "commit that fails Python Validation can ever be pushed."
    )


def test_autofix_is_scoped_to_changed_files() -> None:
    """`--fix` must target fixer-touched paths, never the whole tree.

    A tree-wide auto-fix would repair pre-existing violations inherited
    from main, hiding a regression the terminal gate is meant to surface
    and route to a separate PR.
    """
    text = _workflow_text()
    autofix_at = _step_index(text, _AUTOFIX_STEP)
    gate_at = _step_index(text, _GATE_STEP)
    block = text[autofix_at:gate_at]

    assert "git diff --name-only" in block, (
        "The auto-fix step must derive its file list from `git diff` against "
        "HEAD so it only touches what the fixer wrote."
    )
    assert re.search(r"ruff check --fix[^\n]*CHANGED_PY", block), (
        "`ruff check --fix` must be invoked against the computed changed-file "
        "list, not against directory paths."
    )
    for tree_path in ("app/", "tests/", "scripts/"):
        assert f"--fix -- {tree_path}" not in block, (
            f"The auto-fix step must not run `--fix` across {tree_path}; that "
            "would launder pre-existing lint regressions from main."
        )


def test_autofix_does_not_swallow_unfixable_violations() -> None:
    """Auto-fix must defer the block/allow decision to the terminal gate."""
    text = _workflow_text()
    autofix_at = _step_index(text, _AUTOFIX_STEP)
    gate_at = _step_index(text, _GATE_STEP)
    block = text[autofix_at:gate_at]

    assert "--exit-zero" in block, (
        "The auto-fix pass must use `--exit-zero` so non-auto-fixable "
        "violations flow through to the full-tree gate, which stays the "
        "single place a lint violation blocks the push."
    )


def test_autofix_includes_untracked_python_files() -> None:
    """The auto-fix scope must include untracked files, not only tracked changes.

    The fixer leaves new files unstaged until the host commit step, so they
    are invisible to `git diff HEAD`. Without `git ls-files --others`, a
    fixer-authored Python file with auto-fixable lint still fails the terminal
    gate, preserving the discard failure mode this step was added to prevent.
    """
    text = _workflow_text()
    autofix_at = _step_index(text, _AUTOFIX_STEP)
    gate_at = _step_index(text, _GATE_STEP)
    block = text[autofix_at:gate_at]

    assert "git ls-files --others --exclude-standard" in block, (
        "The auto-fix step must include untracked Python files via "
        "`git ls-files --others --exclude-standard` in addition to "
        "`git diff HEAD`. Fixer-created files are unstaged until the host "
        "commit step and would otherwise be missed."
    )


def test_autofix_skips_during_merge_conflict_repair() -> None:
    """The auto-fix step must skip when MERGE_STATE=conflicts.

    In the conflict-repair path HEAD is the frozen PR SHA while the working
    tree also contains uncommitted main-side merge deltas. Running `ruff --fix`
    in that state would modify main-side files before commit, laundering an
    upstream lint regression into the PR's conflict-resolution commit.
    """
    text = _workflow_text()
    autofix_at = _step_index(text, _AUTOFIX_STEP)
    gate_at = _step_index(text, _GATE_STEP)
    block = text[autofix_at:gate_at]

    assert "MERGE_STATE" in block, (
        "The auto-fix step must expose MERGE_STATE (from sync-main outputs) "
        "so it can guard against running during conflict repair."
    )
    assert "conflicts" in block, (
        "The auto-fix step must skip (exit 0) when MERGE_STATE=conflicts to "
        "avoid laundering main-side lint regressions into the PR commit."
    )


def test_lint_gate_remains_terminal() -> None:
    """The full-tree gate must still hard-fail; auto-fix does not replace it."""
    text = _workflow_text()
    gate_at = _step_index(text, _GATE_STEP)
    commit_at = _step_index(text, _COMMIT_STEP)
    block = text[gate_at:commit_at]

    assert "ruff check app/ tests/ scripts/" in block, (
        "The terminal gate must keep checking the full Python tree, matching "
        "python-validation.yml's ruff scope."
    )
    assert "exit 1" in block, (
        "The terminal gate must still exit non-zero on violations. If it stops "
        "failing, the fixer can push a commit that fails the required check."
    )
