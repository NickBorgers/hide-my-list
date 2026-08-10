"""Schema-level tests for `.github/scripts/review/schema/reviewer-v1.json`.

The reviewer schema is the enforceable contract between reviewer roles and
the read-only judge stage. The two-lens security orchestrator extends it
with two new role values (`security-breadth`, `security-narrow`) and an
optional `summary_metadata` block emitted only by the merger.

These tests pin the contract by validating representative artifacts against
the live schema using `jsonschema` (Draft 2020-12). They cover:

  - Each new lens role validates as a positive case.
  - The merged `role=security` artifact (with `summary_metadata`) validates.
  - An unknown role is rejected (negative).
  - Stray fields under `summary_metadata` are rejected (negative — the
    schema declares `additionalProperties: false`).
  - The pre-orchestrator roles still validate (regression guard).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / ".github" / "scripts" / "review" / "schema" / "reviewer-v1.json"

SHA = "a" * 40


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text())
    return Draft202012Validator(schema)


def _base_artifact(role: str) -> dict:
    return {
        "schema_version": "1",
        "role": role,
        "reviewed_sha": SHA,
        "cycle": 1,
        "decision": "approve",
        "summary": "ok",
        "blocking_issues": [],
        "non_blocking_notes": [],
        "fix_suggestions": [],
        "followup_issues": [],
    }


# ───────────────────────── positive: per-role ─────────────────────────


@pytest.mark.parametrize(
    "role",
    ["design", "security", "security-breadth", "security-narrow", "psych", "docs", "prompt", "test"],
)
def test_each_role_validates(validator: Draft202012Validator, role: str) -> None:
    """Every role in the enum must validate as a minimal valid artifact.

    Acts as a regression guard against accidentally removing or renaming a
    role enum value.
    """
    artifact = _base_artifact(role)
    errors = list(validator.iter_errors(artifact))
    assert errors == [], f"role={role!r} should validate; got: {errors}"


def test_security_breadth_artifact_with_blocker(validator: Draft202012Validator) -> None:
    """Representative breadth lens artifact with a `secb-*` blocker validates."""
    artifact = _base_artifact("security-breadth")
    artifact["decision"] = "request_changes"
    artifact["blocking_issues"] = [
        {
            "id": "secb-001",
            "severity": "high",
            "file": "app/tools/notion.py",
            "line": 42,
            "message": "string-interpolated SQL placeholder",
            "category": "input_validation",
        }
    ]
    artifact["fix_suggestions"] = [
        {
            "id": "secb-001",
            "applicable": "manual",
            "patch_hint": "use psycopg %s placeholders",
            "confidence": 0.9,
        }
    ]
    errors = list(validator.iter_errors(artifact))
    assert errors == [], f"breadth artifact should validate; got: {errors}"


def test_security_narrow_artifact_with_blocker(validator: Draft202012Validator) -> None:
    """Representative narrow lens artifact with a `sec-*` blocker validates."""
    artifact = _base_artifact("security-narrow")
    artifact["decision"] = "request_changes"
    artifact["blocking_issues"] = [
        {
            "id": "sec-001",
            "severity": "high",
            "file": "app/graph/nodes/chat.py",
            "line": 12,
            "message": "new outbound HTTP client outside allowed surface",
            "category": "tool_surface",
            "source": "reviewer",
        }
    ]
    errors = list(validator.iter_errors(artifact))
    assert errors == [], f"narrow artifact should validate; got: {errors}"


def test_merged_security_artifact_with_summary_metadata(validator: Draft202012Validator) -> None:
    """Merged role=security artifact carrying the full summary_metadata block."""
    artifact = _base_artifact("security")
    artifact["decision"] = "request_changes"
    artifact["blocking_issues"] = [
        {
            "id": "sec-001",
            "severity": "high",
            "file": "app/x.py",
            "line": 10,
            "message": "violation of constrained tool surface",
            "category": "tool_surface",
        }
    ]
    artifact["fix_suggestions"] = [
        {"id": "sec-001", "applicable": "manual", "patch_hint": "move to allowed module", "confidence": 0.95}
    ]
    artifact["summary_metadata"] = {
        "merged_from": ["security-breadth", "security-narrow"],
        "truncated_blocking_count": 2,
        "truncated_nonblocking_count": 0,
        "dropped_count": 3,
        "demoted_count": 1,
    }
    errors = list(validator.iter_errors(artifact))
    assert errors == [], f"merged artifact should validate; got: {errors}"


# ───────────────────────── negative cases ─────────────────────────


def test_unknown_role_rejected(validator: Draft202012Validator) -> None:
    artifact = _base_artifact("security-fictitious")
    errors = list(validator.iter_errors(artifact))
    assert errors, "unknown role must be rejected by enum constraint"
    # The error path should point at the role field.
    assert any("role" in [str(p) for p in e.absolute_path] for e in errors), errors


def test_stray_summary_metadata_field_rejected(validator: Draft202012Validator) -> None:
    """summary_metadata declares additionalProperties: false."""
    artifact = _base_artifact("security")
    artifact["summary_metadata"] = {
        "merged_from": ["security-breadth", "security-narrow"],
        "totally_made_up_field": True,
    }
    errors = list(validator.iter_errors(artifact))
    assert errors, "stray field inside summary_metadata must be rejected"


def test_summary_metadata_negative_count_rejected(validator: Draft202012Validator) -> None:
    artifact = _base_artifact("security")
    artifact["summary_metadata"] = {
        "merged_from": ["security-breadth"],
        "truncated_blocking_count": -1,
    }
    errors = list(validator.iter_errors(artifact))
    assert errors, "negative count must be rejected by minimum: 0"


def test_summary_metadata_absence_is_valid(validator: Draft202012Validator) -> None:
    """Other reviewer roles emit no summary_metadata. Absence must validate."""
    artifact = _base_artifact("docs")
    assert "summary_metadata" not in artifact
    errors = list(validator.iter_errors(artifact))
    assert errors == [], f"missing summary_metadata should validate; got: {errors}"


def test_summary_over_500_chars_rejected(validator: Draft202012Validator) -> None:
    """maxLength=500 on summary — regression guard since the merger builds
    a defensively trimmed summary string."""
    artifact = _base_artifact("security")
    artifact["summary"] = "x" * 501
    errors = list(validator.iter_errors(artifact))
    assert errors, "summary > 500 chars must be rejected"
