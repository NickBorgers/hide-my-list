"""Structural lint: fixer host-commit pytest skip wiring.

Asserts that run-required-checks.sh honours HML_SKIP_HOOK_PYTEST, that the
review-fixer.yml host commit step sets it, and that the fixer's tooling
install step does not install pytest (which would imply the unit suite is
expected to run there).

Bug class prevention: the fixer's host-side commit step activates
.githooks/pre-commit via core.hooksPath, which runs `pytest tests/unit/`.
The host runner has no project dependencies installed, so collection died
on `ModuleNotFoundError: No module named 'langchain_core'`, the commit
aborted, and every PR stalled at the fixer stage with all reviewers green.
The unit suite runs as a required check in python-validation.yml instead,
on a read-only runner with the full dependency set and no secrets in scope.
"""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
_FIXER_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "review-fixer.yml"
_SCRIPT = _REPO_ROOT / "scripts" / "run-required-checks.sh"

_SKIP_VAR = "HML_SKIP_HOOK_PYTEST"


def _workflow_text() -> str:
    return _FIXER_WORKFLOW.read_text(encoding="utf-8")


def _script_text() -> str:
    return _SCRIPT.read_text(encoding="utf-8")


def test_script_honours_skip_variable() -> None:
    """run-required-checks.sh must gate the unit suite on the skip variable."""
    assert f'"${{{_SKIP_VAR}:-0}}" = "1"' in _script_text(), (
        f"Expected run_pre_commit_python_checks to check {_SKIP_VAR}. "
        "Without it, the fixer's host commit runs pytest against a runner "
        "that has no project dependencies installed and the commit aborts."
    )


def test_script_still_runs_pytest_by_default() -> None:
    """Absent the skip variable, the unit suite must still run locally."""
    text = _script_text()
    assert "pytest tests/unit/ -x -q" in text, (
        "The default (local developer) pre-commit path must still run the "
        "unit suite. The skip is an opt-in for dependency-less CI contexts."
    )


def test_fixer_commit_step_sets_skip_variable() -> None:
    """The host commit step must set the skip variable to '1'."""
    text = _workflow_text()
    commit_pos = text.find("Commit fixer working-tree changes")
    assert commit_pos != -1, "'Commit fixer working-tree changes' step not found"
    next_step_pos = text.find("\n      - name:", commit_pos + 1)
    commit_block = text[commit_pos:next_step_pos] if next_step_pos != -1 else text[commit_pos:]
    assert f"{_SKIP_VAR}:" in commit_block, (
        f"Expected {_SKIP_VAR} in the 'Commit fixer working-tree changes' step env. "
        "Without it the pre-commit hook runs pytest with no project "
        "dependencies installed and blocks every PR at the fixer stage."
    )
    assert f'{_SKIP_VAR}: "1"' in commit_block, (
        f"Expected {_SKIP_VAR} to be set to \"1\" in the commit step env."
    )


def test_fixer_does_not_install_pytest() -> None:
    """The fixer tooling step must not install pytest.

    Installing it implies the unit suite is meant to run on the host, which
    would additionally require installing the PR's own pyproject.toml —
    executing PR-authored build configuration in the only pipeline stage
    holding WORKFLOW_PAT and contents:write.
    """
    text = _workflow_text()
    install_start = text.find("Install pre-commit hook tooling")
    assert install_start != -1, (
        "Expected an 'Install pre-commit hook tooling' step in review-fixer.yml"
    )
    lint_gate_start = text.find("Ruff lint gate", install_start)
    assert lint_gate_start != -1, "Expected the ruff lint gate to follow the install step"

    install_block = text[install_start:lint_gate_start]
    assert "pytest==" not in install_block, (
        "The fixer install step must not install pytest. The unit suite runs "
        "in python-validation.yml, which has the full dependency set."
    )
