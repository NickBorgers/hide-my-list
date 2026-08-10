"""Structural lint: model tier swap surface integrity.

Two invariants:
  1. setup/model-tiers.json must parse as a JSON object with exactly the keys
     {expensive, medium, cheap, reminder} and non-empty string values.
  2. No Python file in app/ (other than app/models.py) may hardcode a model
     identifier. All routing must go through app/models.py:llm(tier) so that
     swapping a model is a one-line change in setup/model-tiers.json.

Model identifier patterns that count as hardcoded (regex):
  - claude-(opus|sonnet|haiku) — Anthropic model families
  - gpt-                       — OpenAI models
  - gemma                      — Google Gemma models

These patterns are intentionally broad — any string that looks like a
specific model reference outside the designated file is a violation.

The constraint keeps the LiteLLM proxy as the sole dispatch surface.
When the user wants to try a new model, they edit model-tiers.json;
the eval runner rewrites it per session; nothing else changes.

Bug-adjacent to bug class: hardcoded model identifiers bypass tier routing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
_MODEL_TIERS_PATH = _REPO_ROOT / "setup" / "model-tiers.json"
_APP_ROOT = _REPO_ROOT / "app"

# The single file permitted to reference model identifiers directly.
_ALLOWED_FILE = _APP_ROOT / "models.py"

# Required top-level keys in model-tiers.json.
_REQUIRED_KEYS: frozenset[str] = frozenset(["expensive", "medium", "cheap", "reminder"])

# Regex that matches hardcoded model identifiers.
_MODEL_PATTERN = re.compile(
    r"claude-(?:opus|sonnet|haiku)"  # Anthropic families
    r"|gpt-"  # OpenAI
    r"|gemma",  # Google Gemma
    re.IGNORECASE,
)

# Pre-existing hardcoded model identifiers that predate this test.
# Each entry is a (relative_path, line_number) tuple pinning the EXACT location.
# - Removing an entry after cleaning up the source: allowed, update this list.
# - Adding a new entry: blocked; fix the source instead.
# - Any hardcoded model identifier NOT in this set is a new violation and fails.
_KNOWN_VIOLATIONS: frozenset[tuple[str, int]] = frozenset(
    [
        # app/tools/rewards.py uses gpt-image-1 directly for image generation.
        # This bypasses the model-tier swap surface. Cleanup: add an 'image' tier
        # to setup/model-tiers.json and route through app/models.py, or remove
        # image generation from the reward delivery path.
        ("app/tools/rewards.py", 5),    # module docstring listing gpt-image-1
        ("app/tools/rewards.py", 973),  # function docstring referencing gpt-image-1
        ("app/tools/rewards.py", 1079),  # model="gpt-image-1" in generate_reward_image
    ]
)


def _all_app_py_files() -> list[Path]:
    return [
        f
        for f in _APP_ROOT.rglob("*.py")
        if "__pycache__" not in str(f) and f != _ALLOWED_FILE
    ]


def test_model_tiers_json_parses() -> None:
    """setup/model-tiers.json must be valid JSON."""
    assert _MODEL_TIERS_PATH.exists(), f"{_MODEL_TIERS_PATH} not found"
    try:
        data = json.loads(_MODEL_TIERS_PATH.read_text())
    except json.JSONDecodeError as exc:
        pytest.fail(f"setup/model-tiers.json is not valid JSON: {exc}")
    assert isinstance(data, dict), "setup/model-tiers.json must be a JSON object"


def test_model_tiers_json_has_required_keys() -> None:
    """setup/model-tiers.json must have exactly {expensive, medium, cheap, reminder}."""
    data: dict[str, object] = json.loads(_MODEL_TIERS_PATH.read_text())
    actual_keys = frozenset(data.keys())
    missing = _REQUIRED_KEYS - actual_keys
    extra = actual_keys - _REQUIRED_KEYS
    assert not missing, f"setup/model-tiers.json is missing required keys: {sorted(missing)}"
    assert not extra, (
        f"setup/model-tiers.json has unexpected keys: {sorted(extra)}. "
        "Only {expensive, medium, cheap, reminder} are allowed."
    )


def test_model_tiers_json_values_are_nonempty_strings() -> None:
    """Each tier value in setup/model-tiers.json must be a non-empty string."""
    data: dict[str, object] = json.loads(_MODEL_TIERS_PATH.read_text())
    bad: list[str] = []
    for key in _REQUIRED_KEYS:
        val = data.get(key)
        if not isinstance(val, str) or not val.strip():
            bad.append(f"{key!r}: {val!r}")
    assert not bad, f"setup/model-tiers.json has empty or non-string tier values: {bad}"


def test_no_hardcoded_model_identifiers_outside_models_py() -> None:
    """No Python file in app/ (except app/models.py) may hardcode a model identifier.

    All model routing must go through app/models.py:llm(tier). Hardcoded model
    strings outside models.py bypass the swap surface and break model experiments.

    Pre-existing violations from before this test was introduced are listed in
    _KNOWN_VIOLATIONS. New violations (not in that set) always fail.
    """
    violations: list[str] = []
    for filepath in sorted(_all_app_py_files()):
        content = filepath.read_text()
        rel = str(filepath.relative_to(_REPO_ROOT))
        for lineno, line in enumerate(content.splitlines(), start=1):
            # Skip comment lines.
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            matches = _MODEL_PATTERN.findall(line)
            if matches:
                if (rel, lineno) in _KNOWN_VIOLATIONS:
                    continue  # pre-existing; documented above
                violations.append(
                    f"{rel}:{lineno}: hardcoded model identifier(s) {matches!r} in {line.strip()!r}"
                )

    assert not violations, (
        "New hardcoded model identifiers found outside app/models.py. "
        "Use app.models.llm(tier) instead. Pre-existing violations are listed "
        "in _KNOWN_VIOLATIONS in this file; new ones are not allowed:\n"
        + "\n".join(f"  {v}" for v in violations)
    )
