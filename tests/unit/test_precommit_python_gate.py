"""Structural lint: pre-commit Python gate wiring.

Asserts that run-required-checks.sh contains the run_pre_commit_python_checks
function, that it is called from the run_pre_commit dispatcher, that it
invokes ruff and pytest, and that it filters on the .py extension.

Bug class prevention: closes the gap where .py files fell through the
pre-commit dispatcher silently (see PR #579).
"""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "run-required-checks.sh"

FUNCTION_NAME = "run_pre_commit_python_checks"


def _script_text() -> str:
    return _SCRIPT.read_text(encoding="utf-8")


def test_function_is_defined() -> None:
    """The function must exist in the script."""
    assert FUNCTION_NAME in _script_text(), (
        f"Expected '{FUNCTION_NAME}' to be defined in {_SCRIPT}"
    )


def test_function_is_called_from_dispatcher() -> None:
    """run_pre_commit must call run_pre_commit_python_checks."""
    text = _script_text()
    # Find the run_pre_commit() block (stops before the next top-level function).
    # We look for the function call appearing after the run_pre_commit() header
    # and before the closing brace of that block.
    dispatcher_start = text.find("run_pre_commit()")
    assert dispatcher_start != -1, "run_pre_commit() function not found in script"

    # The dispatcher body runs to the next top-level function definition.
    # Locate the call within the text after the dispatcher header.
    dispatcher_region = text[dispatcher_start:]
    assert FUNCTION_NAME in dispatcher_region, (
        f"'{FUNCTION_NAME}' is not called from the run_pre_commit dispatcher"
    )


def test_function_body_runs_ruff() -> None:
    """The function must invoke ruff check."""
    assert "ruff check" in _script_text(), (
        "Expected 'ruff check' in run_pre_commit_python_checks body"
    )


def test_function_body_runs_pytest() -> None:
    """The function must invoke pytest."""
    assert "pytest" in _script_text(), (
        "Expected 'pytest' in run_pre_commit_python_checks body"
    )


def test_function_filters_by_py_extension() -> None:
    """The function must filter staged files by the .py extension pattern."""
    assert r"\.py$" in _script_text(), (
        r"Expected '\.py$' pattern in run_pre_commit_python_checks to filter Python files"
    )
