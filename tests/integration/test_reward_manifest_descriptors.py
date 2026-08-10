"""Integration tests for migration 0011: reward_manifests visual descriptors.

Verifies against a real Postgres that the reward feedback loop closes:

  - write_reward_manifest persists theme_family / style / palette.
  - record_reward_feedback attributes an emoji reaction to that row.
  - load_feedback_history returns the descriptors the reaction earned.
  - _select_theme then weights that theme above its peers.

The unit tests mock the DB, so they can only prove the functions agree with each
other. This proves the columns exist, the INSERT and SELECT column lists match
the migration, and the values survive the round trip — the failure modes a
mocked connection cannot see.

Tests require a live DATABASE_URL. Skipped otherwise.

Private data discipline: all identifiers and content use placeholders; no real
page IDs, recipients, or task titles.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
pytestmark = pytest.mark.skipif(
    not _HAS_DB,
    reason="DATABASE_URL not set; skipping integration tests",
)

_PEER = "<test-peer-manifest-descriptors>"
# Descriptors must be current members of the selection vocabularies, otherwise
# the weighting assertions below have nothing to match against. A stored
# descriptor that is no longer in the vocabulary is inert by construction —
# the same path the NULL-descriptor test covers.
_THEME = "phoenix rising from golden flames"
_STYLE = "watercolor"
_PALETTE = "amber gold"


@pytest.fixture()
async def clean_manifests() -> Any:
    """Apply migrations and clear reward_manifests for this test's peer."""
    import psycopg

    conn_str = os.environ["DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(conn_str, autocommit=False) as conn:
        from app.tools.db import _MIGRATIONS_DIR

        for mig in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await conn.execute(mig.read_text())  # type: ignore[arg-type]
        await conn.commit()

        await conn.execute("DELETE FROM reward_manifests WHERE peer = %s", (_PEER,))
        await conn.commit()
        yield conn

        await conn.execute("DELETE FROM reward_manifests WHERE peer = %s", (_PEER,))
        await conn.commit()


async def _deliver(
    *,
    delivered_at: datetime,
    theme_family: str | None = _THEME,
    style: str | None = _STYLE,
    palette: str | None = _PALETTE,
    reward_kind: str = "emoji+image",
) -> uuid.UUID | None:
    """Write one manifest row through the production write path."""
    from app.tools.rewards import write_reward_manifest

    return await write_reward_manifest(
        peer=_PEER,
        notion_page_id=f"<page-{uuid.uuid4()}>",
        task_title="Placeholder task title",
        reward_kind=reward_kind,
        intensity="high",
        streak_count=1,
        delivered_at=delivered_at,
        artifact_path="/data/reward_artifacts/placeholder.png",
        sensitive_task=False,
        theme_family=theme_family,
        style=style,
        palette=palette,
    )


@pytest.mark.asyncio
async def test_descriptors_round_trip_through_feedback_loop(clean_manifests: Any) -> None:
    """A reaction on a delivered image surfaces that image's descriptors."""
    from app.tools.rewards import load_feedback_history, record_reward_feedback

    delivered_at = datetime.now(UTC)
    manifest_id = await _deliver(delivered_at=delivered_at)
    assert manifest_id is not None, "manifest insert must succeed against the real schema"

    recorded = await record_reward_feedback(
        peer=_PEER,
        emoji="👍",
        target_sent_timestamp=int(delivered_at.timestamp() * 1000),
    )
    assert recorded is True, "reaction within the match window must attach to the reward"

    history = await load_feedback_history(_PEER)
    assert len(history) == 1
    entry = history[0]
    assert entry["score"] == 1
    assert entry["theme_family"] == _THEME
    assert entry["style"] == _STYLE
    assert entry["palette"] == _PALETTE


@pytest.mark.asyncio
async def test_real_feedback_history_biases_theme_selection(clean_manifests: Any) -> None:
    """The loop closes: a real 👍 in Postgres shifts _select_theme's weights.

    This is the assertion that matters. Every other test in this file could pass
    while the feature still did nothing, because the descriptors only earn their
    keep if selection actually consumes them.
    """
    from unittest.mock import patch

    from app.tools import rewards as rewards_module
    from app.tools.rewards import load_feedback_history, record_reward_feedback

    delivered_at = datetime.now(UTC)
    await _deliver(delivered_at=delivered_at)
    await record_reward_feedback(
        peer=_PEER,
        emoji="👍",
        target_sent_timestamp=int(delivered_at.timestamp() * 1000),
    )

    history = await load_feedback_history(_PEER)
    assert history, "history must be non-empty for this test to mean anything"

    # _select_theme draws each axis independently, in theme/style/palette
    # order, so random.choices is called once per axis over that axis'
    # vocabulary.
    captured: list[dict[str, float]] = []

    def fake_choices(
        population: list[str], weights: list[float], k: int = 1
    ) -> list[str]:
        total = sum(weights)
        captured.append({v: w / total for v, w in zip(population, weights, strict=True)})
        return [population[0]]

    with patch.object(rewards_module.random, "choices", fake_choices):
        rewards_module._select_theme(intensity="high", feedback_history=history)

    assert len(captured) == 3, "each axis must be drawn independently"
    theme_probs, style_probs, palette_probs = captured

    others = [p for theme, p in theme_probs.items() if theme != _THEME]
    assert theme_probs[_THEME] > max(others), (
        "a real positive reaction stored in Postgres must raise that theme's odds"
    )
    assert all(p > 0 for p in theme_probs.values()), "no theme may be excluded outright"

    # The reaction was recorded against a style and palette too, so the same
    # stored row must lift those axes as well. This is what the descriptors are
    # for: one reaction is evidence about three things, not one.
    assert style_probs[_STYLE] > max(p for s, p in style_probs.items() if s != _STYLE)
    assert palette_probs[_PALETTE] > max(
        p for s, p in palette_probs.items() if s != _PALETTE
    )


@pytest.mark.asyncio
async def test_emoji_only_reward_stores_null_descriptors(clean_manifests: Any) -> None:
    """Rewards with no image store NULL descriptors, read back as empty strings.

    Rows written before migration 0011 are NULL for the same reason, so this
    also covers the historical-row path: they must never match a candidate.
    """
    from app.tools.rewards import load_feedback_history, record_reward_feedback

    delivered_at = datetime.now(UTC)
    await _deliver(
        delivered_at=delivered_at,
        theme_family=None,
        style=None,
        palette=None,
        reward_kind="emoji",
    )
    await record_reward_feedback(
        peer=_PEER,
        emoji="👍",
        target_sent_timestamp=int(delivered_at.timestamp() * 1000),
    )

    history = await load_feedback_history(_PEER)
    assert len(history) == 1
    assert history[0]["theme_family"] == ""
    assert history[0]["style"] == ""
    assert history[0]["palette"] == ""
