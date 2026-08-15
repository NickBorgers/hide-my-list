"""Banned shame-triggering phrase catalog.

Sourced from `docs/ai-prompts/shared.md` (SHAME PREVENTION) and
`design/adhd-priorities.md`. Lives here rather than inside a single test module
so every layer scores delivered text against the same list — a phrase banned in
the prompt gate but unchecked in a conversation chain is a gap, not a policy.

Regex only. Judge-scored shame-safety (tone, framing, implicature) belongs to
the eval layer, which has a model available to score it.
"""
from __future__ import annotations

import re

BANNED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\byou didn'?t\b", re.IGNORECASE),
    re.compile(r"\byou should have\b", re.IGNORECASE),
    re.compile(r"\byou forgot\b", re.IGNORECASE),
    re.compile(r"\byou failed\b", re.IGNORECASE),
    re.compile(r"\byou never\b", re.IGNORECASE),
    re.compile(r"\byou haven'?t\b", re.IGNORECASE),
    re.compile(r"\byou missed\b", re.IGNORECASE),
    re.compile(r"\bfailed to\b", re.IGNORECASE),
    re.compile(r"\byou were supposed to\b", re.IGNORECASE),
    re.compile(r"\byou were meant to\b", re.IGNORECASE),
    re.compile(r"\byou are lazy\b", re.IGNORECASE),
    re.compile(r"\byou're lazy\b", re.IGNORECASE),
)


def find_banned_phrases(text: str) -> list[str]:
    """Return every banned phrase found in `text`, as matched substrings."""
    found: list[str] = []
    for pattern in BANNED_PATTERNS:
        found.extend(match.group(0) for match in pattern.finditer(text))
    return found
