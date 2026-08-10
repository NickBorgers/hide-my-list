"""Structural lint: REQUIRED_PROPERTIES covers every property the client uses.

verify_schema() is only as good as its declaration. If someone adds a verb
filtering on a new Notion property and does not add that property to
REQUIRED_PROPERTIES, the schema probe reports a healthy database while the
new verb 400s — reintroducing exactly the blind spot the probe exists to
close, and doing it silently.

This lint parses app/tools/notion.py and extracts every property name the
module references as a literal, from three syntactic positions:

  1. filter and sort entries:   {"property": "Due At", ...}
  2. page payload dict literals: {"Due At": {"date": {...}}}
  3. conditional payload writes: props["Inline Steps"] = {"rich_text": ...}

Every extracted name must appear in REQUIRED_PROPERTIES.

update_property() is exempt by construction: its property name is a function
parameter, not a literal, so it is invisible to this scan and cannot be
covered by a static declaration.

Standalone run: pytest tests/unit/test_notion_required_properties.py -v
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.tools.notion import REQUIRED_PROPERTIES

_NOTION_SOURCE = (
    Path(__file__).parent.parent.parent / "app" / "tools" / "notion.py"
)

#: Notion property type keys as they appear in page payloads. A dict whose
#: values are all single-key dicts drawn from this set is a properties map.
_NOTION_TYPE_KEYS = frozenset(
    {
        "title",
        "rich_text",
        "number",
        "select",
        "multi_select",
        "date",
        "checkbox",
        "relation",
        "people",
        "url",
        "email",
        "phone_number",
        "files",
        "status",
    }
)


def _is_property_value(node: ast.expr) -> bool:
    """True if `node` looks like a Notion property value, e.g. {"date": {...}}."""
    if not isinstance(node, ast.Dict):
        return False
    return any(
        isinstance(key, ast.Constant) and key.value in _NOTION_TYPE_KEYS
        for key in node.keys
    )


def _referenced_properties() -> set[str]:
    """Extract every literal Notion property name referenced in notion.py."""
    tree = ast.parse(_NOTION_SOURCE.read_text(encoding="utf-8"))
    found: set[str] = set()

    for node in ast.walk(tree):
        # Position 1 — {"property": "Due At", ...} in filters and sorts.
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "property"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    found.add(value.value)

            # Position 2 — properties map literal: {"Due At": {"date": {...}}}.
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and _is_property_value(value)
                ):
                    found.add(key.value)

        # Position 3 — props["Inline Steps"] = {"rich_text": [...]}.
        if isinstance(node, ast.Assign) and _is_property_value(node.value):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    found.add(target.slice.value)

    return found


def test_extraction_finds_the_known_properties() -> None:
    """The scan itself works — guards against a silently-empty lint.

    A parser change or refactor that broke extraction would make the coverage
    test below pass vacuously. Anchor on properties that must always exist.
    """
    referenced = _referenced_properties()

    assert len(referenced) >= 10, f"suspiciously few properties found: {referenced}"
    for anchor in ("Due At", "Reminder Scheduled At", "Is Reminder", "Status"):
        assert anchor in referenced, f"extraction missed a known property: {anchor}"


def test_every_referenced_property_is_declared() -> None:
    """Any property the client reads or writes must be in REQUIRED_PROPERTIES."""
    undeclared = _referenced_properties() - set(REQUIRED_PROPERTIES)

    assert not undeclared, (
        f"Notion properties used in app/tools/notion.py but absent from "
        f"REQUIRED_PROPERTIES: {sorted(undeclared)}. Add them, or the schema "
        f"probe will report a healthy database while these verbs fail."
    )


@pytest.mark.parametrize("name,prop_type", sorted(REQUIRED_PROPERTIES.items()))
def test_declared_types_are_valid_notion_types(name: str, prop_type: str) -> None:
    """Each declared type is a real Notion property type.

    A typo'd type would make verify_schema() report a permanent mismatch on a
    correct database — a false alarm that trains the operator to ignore it.
    """
    assert prop_type in _NOTION_TYPE_KEYS, f"{name}: unknown Notion type {prop_type!r}"
