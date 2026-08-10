"""Structural lint: mypy ignore_errors suppression budget.

Counts [[tool.mypy.overrides]] blocks with ignore_errors = true in
pyproject.toml and asserts the count matches a frozen baseline.

The baseline represents a "debt ceiling": anyone who adds a new
suppressed module must update BASELINE_COUNT and add the module name
to BASELINE_MODULES, which forces a deliberate decision and a comment
in their PR explaining why the suppression is necessary.

The baseline can only decrease — if issue #560 or other mypy-cleanup
work removes a suppression, lower the baseline so it cannot creep back.

Currently the codebase has 0 ignore_errors overrides. The baseline was
set to 0 when this test was introduced; there are no outstanding mypy
suppressions. Any addition requires updating both constants below.

Bug class 8: mypy ignore_errors suppression sprawl.
"""
from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Frozen baseline — update both constants together when a module is added
# or removed. The baseline is the exact set of modules permitted to use
# ignore_errors = true. Adding a new entry requires a PR comment explaining
# the suppression rationale and a plan for removal.
# ---------------------------------------------------------------------------

BASELINE_COUNT: int = 0
"""Number of [[tool.mypy.overrides]] blocks with ignore_errors = true."""

BASELINE_MODULES: frozenset[str] = frozenset()
"""Exact module names that are permitted to suppress mypy errors.

When this was introduced (PR #<this PR>), the count was 0 because all
eight previously suppressed modules had already been cleaned up by
issue #560 work before this test was written.
"""

# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _parse_suppressed_modules(text: str) -> list[str]:
    """Return module names from [[tool.mypy.overrides]] with ignore_errors = true.

    Uses a simple regex parser instead of tomllib to avoid needing Python 3.11+
    or an import that may not be available in all CI environments. The format
    is deterministic: toml arrays of tables always emit the full key path.
    """
    # Find override blocks: [[tool.mypy.overrides]] ... next [[...]] or end
    override_pattern = re.compile(
        r"\[\[tool\.mypy\.overrides\]\](.*?)(?=\[\[|\Z)",
        re.DOTALL,
    )
    module_pattern = re.compile(r'module\s*=\s*["\']([^"\']+)["\']')
    ignore_pattern = re.compile(r"ignore_errors\s*=\s*true", re.IGNORECASE)

    suppressed: list[str] = []
    for block in override_pattern.finditer(text):
        body = block.group(1)
        if ignore_pattern.search(body):
            m = module_pattern.search(body)
            if m:
                suppressed.append(m.group(1))
    return suppressed


def test_mypy_suppression_count_matches_baseline() -> None:
    """[[tool.mypy.overrides]] blocks with ignore_errors = true must not exceed baseline."""
    text = _PYPROJECT.read_text()
    suppressed = _parse_suppressed_modules(text)

    assert len(suppressed) == BASELINE_COUNT, (
        f"mypy suppression count changed. Expected {BASELINE_COUNT}, "
        f"found {len(suppressed)}.\n"
        f"Current suppressed modules: {sorted(suppressed)}\n"
        f"Baseline modules: {sorted(BASELINE_MODULES)}\n\n"
        "If you are REMOVING a suppression (good!), lower BASELINE_COUNT and "
        "remove the module from BASELINE_MODULES in this test file.\n"
        "If you are ADDING a suppression (debt!), raise BASELINE_COUNT and "
        "add the module to BASELINE_MODULES, then explain the suppression in "
        "your PR description with a plan for eventual removal."
    )


def test_mypy_suppression_modules_match_baseline() -> None:
    """Suppressed modules must exactly match the BASELINE_MODULES set."""
    text = _PYPROJECT.read_text()
    suppressed = frozenset(_parse_suppressed_modules(text))

    added = suppressed - BASELINE_MODULES
    removed = BASELINE_MODULES - suppressed

    messages: list[str] = []
    if added:
        messages.append(
            f"New suppressed modules (not in baseline): {sorted(added)}. "
            "Add them to BASELINE_MODULES and raise BASELINE_COUNT."
        )
    if removed:
        messages.append(
            f"Modules removed from suppression (good!): {sorted(removed)}. "
            "Remove them from BASELINE_MODULES and lower BASELINE_COUNT."
        )

    assert not messages, "\n".join(messages)
