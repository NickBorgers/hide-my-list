"""Structural lint: regression catalog hygiene.

Walks tests/regressions/ and for every bug_* directory asserts:
  1. README.md exists.
  2. README.md content contains a GitHub issue or PR reference (#NNN).
  3. At least one .py test file exists in the directory, OR the README
     explicitly notes that the test lives elsewhere (phrase "test lives in"
     or similar).
  4. If the README names a test path with "test lives in/at `<path>`",
     that path must exist in the current tree.

This ensures the regression catalog stays coherent: every bug directory has
a story (README), a canonical issue/PR link, and either a real test or an
explicit pointer to an existing test at another layer.

Standalone run: pytest tests/unit/test_regression_catalog.py -v
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
_REGRESSIONS_DIR = _REPO_ROOT / "tests" / "regressions"

_ISSUE_PATTERN = re.compile(r"#\d+")
_ELSEWHERE_PHRASES = (
    "test lives in",
    "test lives at",
    "tests live in",
    "tests live at",
)
_PATH_AFTER_LIVES_IN = re.compile(
    r"tests? lives? (?:in|at)\s+`([^`]+)`",
    re.IGNORECASE,
)


def _bug_dirs() -> list[Path]:
    """All directories matching tests/regressions/bug_*."""
    if not _REGRESSIONS_DIR.exists():
        return []
    return sorted(d for d in _REGRESSIONS_DIR.iterdir() if d.is_dir() and d.name.startswith("bug_"))


def _has_test_elsewhere(readme_text: str) -> bool:
    """Return True if the README explicitly says the test lives elsewhere."""
    lower = readme_text.lower()
    return any(phrase in lower for phrase in _ELSEWHERE_PHRASES)


def _referenced_paths(readme_text: str) -> list[str]:
    """Extract file paths mentioned after 'test lives in/at' in backticks."""
    return _PATH_AFTER_LIVES_IN.findall(readme_text)


@pytest.mark.parametrize("bug_dir", _bug_dirs(), ids=lambda d: d.name)
def test_regression_directory_is_well_formed(bug_dir: Path) -> None:
    """Each bug_* directory must have a README, a GH reference, and a test (or pointer)."""
    readme = bug_dir / "README.md"
    assert readme.exists(), (
        f"{bug_dir.name}/README.md is missing. Every regression directory must have "
        f"a README.md with a 3-line bug story and a GitHub issue/PR reference."
    )

    readme_text = readme.read_text()

    assert _ISSUE_PATTERN.search(readme_text), (
        f"{bug_dir.name}/README.md does not contain a GitHub issue or PR reference "
        f"(pattern: #NNN). Add the canonical issue or PR number that tracks this bug."
    )

    py_files = [
        f for f in bug_dir.iterdir()
        if f.name.startswith("test_") and f.suffix == ".py"
    ]
    has_test = bool(py_files)
    has_elsewhere = _has_test_elsewhere(readme_text)

    assert has_test or has_elsewhere, (
        f"{bug_dir.name} has no test_*.py file and README.md does not note that "
        f"the test lives elsewhere. Either add a test_*.py or add a note like "
        f'"test lives in tests/evals/..." to the README.'
    )

    # If README says test lives elsewhere, require a concrete backtick path pointer.
    referenced = _referenced_paths(readme_text)
    if not has_test and has_elsewhere:
        assert referenced, (
            f"{bug_dir.name}/README.md says the test lives elsewhere but names no "
            f"backtick path. Use 'test lives in `tests/path/to/test.py`' format; "
            f"bare phrases like 'test lives in PR-2' are not sufficient."
        )
    missing = [p for p in referenced if not (_REPO_ROOT / p).exists()]
    assert not missing, (
        f"{bug_dir.name}/README.md references test paths that do not exist: {missing}. "
        "Either add the missing tests or remove the path claim until the tests land."
    )


def test_regressions_dir_exists() -> None:
    """The tests/regressions/ directory must exist."""
    assert _REGRESSIONS_DIR.exists() and _REGRESSIONS_DIR.is_dir(), (
        f"tests/regressions/ directory not found at {_REGRESSIONS_DIR}. "
        "Create it and seed it with at least one bug_* directory."
    )
