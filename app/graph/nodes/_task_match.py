"""Shared lexical matching between free text and open Notion tasks.

Two nodes need to decide whether a phrase the user typed refers to a task that
already exists: intake asks "is this new task a duplicate of one on the list?",
and complete asks "which task on the list did the user just finish?". Both run
the same two-stage pipeline — a cheap token-overlap shortlist that favors recall,
then a model call that adjudicates the shortlist — so the shortlist, the Notion
property extraction, and the response parsing live here.

Why this design: the shortlist score is a recall device only. It exists to keep
the model's candidate set small and its prompt cheap; it never authorizes an
action on its own. Each caller owns its own prompt, its own confidence
threshold, and its own decision about what to do with a match, because the cost
of a false match differs sharply between them.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

OPEN_TASK_STATUSES = {"Pending", "In Progress"}
MAX_CANDIDATES = 5
MIN_SCORE = 0.4

# Words carrying no distinguishing signal in either a task title or a message
# about one. Stripped from both sides of every comparison.
STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "by",
    "do",
    "for",
    "i",
    "in",
    "it",
    "me",
    "my",
    "need",
    "of",
    "on",
    "please",
    "the",
    "to",
    "with",
}


@dataclass(frozen=True)
class DedupCandidate:
    """Shortlisted existing task that may describe the proposed task."""

    page_id: str
    title: str
    score: float


def shortlist_duplicate_candidates(
    proposed_title: str,
    existing_tasks: list[Mapping[str, str]],
    *,
    limit: int = MAX_CANDIDATES,
    min_score: float = MIN_SCORE,
    query_stopwords: frozenset[str] = frozenset(),
) -> list[DedupCandidate]:
    """Return likely matching candidates using token overlap only.

    `query_stopwords` is subtracted from `proposed_title`'s tokens and never
    from a candidate title's. Callers matching a whole user message rather than
    a task-shaped phrase use it to drop filler that is meaningless in a message
    but meaningful in a title — "out" says nothing in "knocked that out" and
    everything in "Take out the trash".
    """
    proposed_tokens = normalize_title_tokens(proposed_title) - query_stopwords
    if not proposed_tokens or not existing_tasks:
        return []

    candidates: list[DedupCandidate] = []
    for task in existing_tasks:
        page_id = task.get("id", "")
        title = task.get("title", "")
        if not page_id or not title:
            continue
        title_tokens = normalize_title_tokens(title)
        if not title_tokens:
            continue
        score = dice_coefficient(proposed_tokens, title_tokens)
        if score >= min_score:
            candidates.append(DedupCandidate(page_id=page_id, title=title, score=score))

    return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)[:limit]


def dice_coefficient(left: set[str], right: set[str]) -> float:
    """Return the Sørensen–Dice overlap of two token sets, 0.0 when either is empty."""
    if not left or not right:
        return 0.0
    return (2 * len(left & right)) / (len(left) + len(right))


def normalize_title_tokens(title: str) -> set[str]:
    """Normalize a task title into comparable non-stopword tokens."""
    normalized = "".join(
        " " if unicodedata.category(char).startswith("P") else char.casefold()
        for char in title
    )
    tokens = set()
    for raw_token in normalized.split():
        token = raw_token.strip()
        if len(token) < 2 or token in STOPWORDS:
            continue
        tokens.add(token)
    return tokens


def open_non_reminder_tasks(query_all_response: Mapping[str, Any]) -> list[Mapping[str, str]]:
    """Extract open, non-reminder task ids and titles from a Notion query response."""
    results = query_all_response.get("results", [])
    if not isinstance(results, list):
        return []
    tasks: list[Mapping[str, str]] = []
    for page in results:
        if not isinstance(page, dict):
            continue
        props = page.get("properties", {})
        if not isinstance(props, dict):
            continue
        status = extract_select(props, "Status")
        is_reminder = extract_checkbox(props, "Is Reminder")
        if status not in OPEN_TASK_STATUSES or is_reminder:
            continue
        page_id = page.get("id", "")
        title = extract_title(props)
        if isinstance(page_id, str) and page_id and title:
            tasks.append({"id": page_id, "title": title})
    return tasks


def parse_match_response(
    response_text: str,
    candidates: list[DedupCandidate],
) -> tuple[str, float] | None:
    """Parse a `{"matched_page_id", "confidence"}` adjudication into a validated pair.

    Returns None for anything the caller must not act on: unparseable output, a
    null match, or a page id the model invented rather than picked from
    `candidates`.
    """
    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if not json_match:
        return None
    try:
        loaded = json.loads(json_match.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, dict):
        return None
    matched_page_id = loaded.get("matched_page_id")
    confidence_raw = loaded.get("confidence")
    if not isinstance(matched_page_id, str):
        return None
    if not isinstance(confidence_raw, int | float | str):
        return None
    try:
        confidence = float(confidence_raw)
    except ValueError:
        return None
    candidate_ids = {candidate.page_id for candidate in candidates}
    if matched_page_id not in candidate_ids:
        return None
    return matched_page_id, confidence


def extract_title(props: dict[str, Any]) -> str:
    """Extract the title string from a Notion page properties dict."""
    title_prop = props.get("Title", {})
    if not isinstance(title_prop, dict):
        return ""
    items = title_prop.get("title", [])
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for item in items:
        if isinstance(item, dict):
            plain_text = item.get("plain_text", "")
            if isinstance(plain_text, str):
                parts.append(plain_text)
    return "".join(parts)


def extract_select(props: dict[str, Any], key: str) -> str:
    """Extract a select property value."""
    prop = props.get(key, {})
    if not isinstance(prop, dict):
        return ""
    sel = prop.get("select") or {}
    if not isinstance(sel, dict):
        return ""
    name = sel.get("name", "")
    return name if isinstance(name, str) else ""


def extract_checkbox(props: dict[str, Any], key: str) -> bool:
    """Extract a checkbox property value."""
    prop = props.get(key, {})
    if not isinstance(prop, dict):
        return False
    value = prop.get("checkbox", False)
    return bool(value)
