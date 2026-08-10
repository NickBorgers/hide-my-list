"""Structural lint: signal-cli image pin and its refresh workflow.

signal-cli is the only path for inbound and outbound Signal messages, so its
pin is a production dependency rather than CI tooling. Two properties have to
hold together:

  1. The pin is an immutable digest, so deploys are reproducible.
  2. Something refreshes that digest, so it cannot silently rot.

Bug class prevention: the pin sat at a 2025-05-15 digest for 14 months with no
refresh path. Signal changed its server-side envelope format in that window and
the pinned signal-cli threw NullPointerException on every inbound message,
discarding it before it reached the app. Inbound Signal was dead for seven
weeks. Property 1 without property 2 is what caused that.

The workflow rewrites `docker/compose.yaml` by exact string match on the image
line and by regex on the provenance comment, so the shape of both is a contract
between these two files — not cosmetic.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
_COMPOSE = _REPO_ROOT / "docker" / "compose.yaml"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "update-signal-cli.yml"

_IMAGE_REPO = "bbernhard/signal-cli-rest-api"


def _compose_text() -> str:
    return _COMPOSE.read_text(encoding="utf-8")


def test_signal_cli_pinned_by_digest() -> None:
    """The image must be pinned by sha256 digest, never a mutable tag."""
    text = _compose_text()
    assert re.search(rf"image:\s*{re.escape(_IMAGE_REPO)}@sha256:[0-9a-f]{{64}}", text), (
        f"Expected '{_IMAGE_REPO}' to be pinned by sha256 digest in docker/compose.yaml. "
        "A mutable tag makes deploys non-reproducible; the refresh workflow also "
        "reads the digest from this line and will fail without it."
    )


def test_signal_cli_pin_is_not_a_mutable_tag() -> None:
    """Guard the specific regression of switching back to :latest or a tag."""
    text = _compose_text()
    assert not re.search(rf"image:\s*{re.escape(_IMAGE_REPO)}:[A-Za-z0-9._-]+", text), (
        f"'{_IMAGE_REPO}' appears pinned to a tag rather than a digest. "
        "Tags are mutable — use image@sha256:<digest>."
    )


def test_provenance_comment_matches_workflow_contract() -> None:
    """Exactly one '# Pinned: <date> against <repo>:latest' comment must exist.

    update-signal-cli.yml rewrites this line with a regex and hard-fails unless
    it matches exactly once. Reformatting it here silently breaks the refresh.
    """
    text = _compose_text()
    matches = re.findall(
        rf"# Pinned: \d{{4}}-\d{{2}}-\d{{2}} against {re.escape(_IMAGE_REPO)}:latest",
        text,
    )
    assert len(matches) == 1, (
        f"Expected exactly one '# Pinned: YYYY-MM-DD against {_IMAGE_REPO}:latest' "
        f"comment in docker/compose.yaml, found {len(matches)}. The refresh "
        "workflow rewrites this line and fails closed when the count is not 1."
    )


def test_refresh_workflow_exists() -> None:
    """A workflow must refresh the pin; a digest with no upgrade path rots."""
    assert _WORKFLOW.is_file(), (
        "Expected .github/workflows/update-signal-cli.yml. Pinning by digest "
        "without an automated refresh is what left signal-cli 14 months stale "
        "and inbound Signal broken for seven weeks."
    )


def test_refresh_workflow_targets_the_pinned_image() -> None:
    """The workflow must reference the same image the compose file pins."""
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    assert _IMAGE_REPO in workflow, (
        f"update-signal-cli.yml must reference '{_IMAGE_REPO}' — the image "
        "docker/compose.yaml pins."
    )


def test_refresh_workflow_is_scheduled() -> None:
    """A manual-only refresh is not a refresh; it must run on a schedule."""
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    assert "schedule:" in workflow and "cron:" in workflow, (
        "update-signal-cli.yml must have a cron schedule. The failure mode "
        "being prevented is nobody remembering to check."
    )


def test_refresh_workflow_validates_digest_before_writing() -> None:
    """The workflow must reject a malformed digest rather than write it."""
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    assert "sha256:[0-9a-f]{64}" in workflow, (
        "update-signal-cli.yml must validate the registry's response against a "
        "digest pattern before writing it into docker/compose.yaml. Writing an "
        "unvalidated upstream value into a deploy manifest is the fail-open case."
    )
