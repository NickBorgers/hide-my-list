"""Structural lint: v2 fixer prompts must not name the output file in prose.

The fixer schema is named `fix-result-v1.json` and the fixer-role action sets
`OUTPUT_PATH=.review-output/fixer-result.json`. Prose like "Write fix-result
JSON to $OUTPUT_PATH" reads as a filename hint to the model, which has caused
it to write `fix-result.json` instead of resolving the env var. The host then
fails to find the expected file and the review pipeline stalls at NO-GO with
no human-actionable signal.

These tests enforce the lessons:
  1. No fixer prompt may contain the phrase `fix-result JSON` (the bug-prone
     phrasing that caused the original incident).
  2. The output contract section must reference `$OUTPUT_PATH` and must not
     hard-code a literal `.review-output/...result.json` path. Hard-coding a
     literal filename anywhere near the contract block invites the same
     misread.

Scope: `.github/scripts/review/prompts/fixer*.md`. Reviewer prompts don't
exhibit this bug class (different schema basename, different prose) and are
out of scope.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
_PROMPTS_DIR = _REPO_ROOT / ".github" / "scripts" / "review" / "prompts"

# Matches "fix-result JSON" with arbitrary whitespace between the two tokens,
# case-insensitive. Crucially does NOT match the schema reference
# "fix-result-v1.json" — that has a hyphenated version suffix, not whitespace.
_BUG_PRONE_PHRASE = re.compile(r"\bfix-result\s+JSON\b", re.IGNORECASE)

# Matches any hard-coded .review-output/*.json filename in the prompt prose.
# The contract should always reference $OUTPUT_PATH instead.
_HARD_CODED_OUTPUT_PATH = re.compile(r"\.review-output/[A-Za-z0-9_-]+\.json")


def _fixer_prompt_files() -> list[Path]:
    return sorted(_PROMPTS_DIR.glob("fixer*.md"))


def test_fixer_prompts_exist() -> None:
    """Guard against accidentally moving or deleting the prompts this lint covers."""
    files = _fixer_prompt_files()
    names = {f.name for f in files}
    assert "fixer.md" in names, f"fixer.md missing from {_PROMPTS_DIR}"
    assert "fixer-resume.md" in names, f"fixer-resume.md missing from {_PROMPTS_DIR}"


@pytest.mark.parametrize("prompt_path", _fixer_prompt_files(), ids=lambda p: p.name)
def test_no_bug_prone_phrase(prompt_path: Path) -> None:
    """`fix-result JSON` reads as a filename hint; ban it from fixer prompts."""
    text = prompt_path.read_text()
    matches = _BUG_PRONE_PHRASE.findall(text)
    assert not matches, (
        f"{prompt_path.name} contains the bug-prone phrase 'fix-result JSON' "
        f"({len(matches)} occurrence(s)). The model latches onto this as a "
        f"filename and writes fix-result.json instead of $OUTPUT_PATH. Replace "
        f"with 'the result as JSON' or similar."
    )


@pytest.mark.parametrize("prompt_path", _fixer_prompt_files(), ids=lambda p: p.name)
def test_no_hard_coded_output_filename(prompt_path: Path) -> None:
    """The output contract must reference $OUTPUT_PATH, not a literal path."""
    text = prompt_path.read_text()
    matches = _HARD_CODED_OUTPUT_PATH.findall(text)
    assert not matches, (
        f"{prompt_path.name} hard-codes an output filename: {matches!r}. "
        f"This conflicts with $OUTPUT_PATH and risks the same misread that "
        f"caused PR #574 to stall. Reference $OUTPUT_PATH only."
    )


@pytest.mark.parametrize("prompt_path", _fixer_prompt_files(), ids=lambda p: p.name)
def test_output_contract_references_output_path(prompt_path: Path) -> None:
    """The output-write instruction must reference $OUTPUT_PATH.

    A prompt that avoids 'fix-result JSON' and hard-coded paths but also
    omits '$OUTPUT_PATH' from the write instruction still leaves the
    same artifact-discovery failure mode open: the agent has no canonical
    destination and may invent a filename.
    """
    text = prompt_path.read_text()
    assert "$OUTPUT_PATH" in text, (
        f"{prompt_path.name} does not reference $OUTPUT_PATH. "
        f"The output contract section must instruct the agent to write the "
        f"result JSON to $OUTPUT_PATH so the host runner can locate the artifact."
    )
