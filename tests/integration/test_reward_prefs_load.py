"""Integration tests for load_reward_prefs against a real Postgres.

The reward taste profile lives at `user_prefs.prefs_json -> 'rewards'`. Until
now nothing in app/ read the user_prefs table at all, so no test — unit or
integration — had ever exercised that column against a real database.

The unit tests mock the connection, so they can only prove the function agrees
with a fake. This proves the table exists with the column name the query uses,
that a nested JSON subtree survives the psycopg jsonb round trip as a dict
rather than a string, and that the absent-row and malformed-shape paths return
{} instead of raising — the failure modes a mocked connection cannot see.

Tests require a live DATABASE_URL. Skipped otherwise.

Private data discipline: all identifiers and preference values are
placeholders; no real recipients or user-authored preference text.
"""
from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
pytestmark = pytest.mark.skipif(
    not _HAS_DB,
    reason="DATABASE_URL not set; skipping integration tests",
)

_PEER = "<test-peer-reward-prefs>"


@pytest.fixture()
async def clean_prefs() -> Any:
    """Apply migrations and clear user_prefs for this test's peer."""
    import psycopg

    conn_str = os.environ["DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(conn_str, autocommit=False) as conn:
        from app.tools.db import _MIGRATIONS_DIR

        for mig in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await conn.execute(mig.read_text())  # type: ignore[arg-type]
        await conn.commit()

        await conn.execute("DELETE FROM user_prefs WHERE peer = %s", (_PEER,))
        await conn.commit()
        yield conn

        await conn.execute("DELETE FROM user_prefs WHERE peer = %s", (_PEER,))
        await conn.commit()


async def _write_prefs(conn: Any, prefs_json: str) -> None:
    """Insert a user_prefs row carrying the given raw JSON."""
    await conn.execute(
        """
        INSERT INTO user_prefs (peer, prefs_json)
        VALUES (%s, %s::jsonb)
        ON CONFLICT (peer) DO UPDATE SET prefs_json = EXCLUDED.prefs_json
        """,
        (_PEER, prefs_json),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_rewards_subtree_round_trips(clean_prefs: Any) -> None:
    """A stored rewards subtree comes back as a dict with its lists intact."""
    from app.tools.rewards import load_reward_prefs

    stored = {
        "rewards": {
            "preferred_styles": ["placeholder style one", "placeholder style two"],
            "preferred_palettes": ["placeholder palette"],
            "avoid": ["placeholder avoided subject"],
            "humor_level": "subtle",
        },
        "timezone": "America/Chicago",
    }
    await _write_prefs(clean_prefs, json.dumps(stored))

    prefs = await load_reward_prefs(_PEER)

    # jsonb must decode to real Python structures, not a JSON string.
    assert isinstance(prefs, dict)
    assert prefs["preferred_styles"] == ["placeholder style one", "placeholder style two"]
    assert prefs["preferred_palettes"] == ["placeholder palette"]
    assert prefs["avoid"] == ["placeholder avoided subject"]
    assert prefs["humor_level"] == "subtle"
    # Only the rewards subtree is returned; sibling keys stay out.
    assert "timezone" not in prefs


@pytest.mark.asyncio
async def test_missing_peer_returns_empty(clean_prefs: Any) -> None:
    """A peer with no preferences row is not an error."""
    from app.tools.rewards import load_reward_prefs

    assert await load_reward_prefs(_PEER) == {}


@pytest.mark.asyncio
async def test_row_without_rewards_subtree_returns_empty(clean_prefs: Any) -> None:
    """Preferences that exist but carry no rewards subtree return {}."""
    from app.tools.rewards import load_reward_prefs

    await _write_prefs(clean_prefs, json.dumps({"timezone": "America/Chicago"}))

    assert await load_reward_prefs(_PEER) == {}


@pytest.mark.asyncio
async def test_non_object_rewards_subtree_returns_empty(clean_prefs: Any) -> None:
    """A rewards value of the wrong JSON type degrades to {} rather than raising.

    jsonb accepts scalars and arrays, so the column type alone does not
    guarantee the shape the reward path expects.
    """
    from app.tools.rewards import load_reward_prefs

    await _write_prefs(clean_prefs, json.dumps({"rewards": ["not", "an", "object"]}))
    assert await load_reward_prefs(_PEER) == {}

    await _write_prefs(clean_prefs, json.dumps({"rewards": "not an object"}))
    assert await load_reward_prefs(_PEER) == {}


@pytest.mark.asyncio
async def test_non_object_prefs_json_returns_empty(clean_prefs: Any) -> None:
    """A whole prefs_json that is not an object degrades to {}."""
    from app.tools.rewards import load_reward_prefs

    await _write_prefs(clean_prefs, json.dumps(["not", "an", "object"]))

    assert await load_reward_prefs(_PEER) == {}


@pytest.mark.asyncio
async def test_loaded_prefs_reach_theme_selection(clean_prefs: Any) -> None:
    """The stored profile actually steers _select_theme.

    Closes the loop this PR exists to close: before it, preferences were read
    by nothing, so a stored style could never appear in a generated image.
    """
    from app.tools.rewards import _select_theme, load_reward_prefs

    await _write_prefs(
        clean_prefs,
        json.dumps(
            {
                "rewards": {
                    "preferred_styles": ["placeholder distinctive style"],
                    "preferred_palettes": ["placeholder distinctive palette"],
                }
            }
        ),
    )

    prefs = await load_reward_prefs(_PEER)

    # Preferences bias the draw rather than replacing the vocabulary, so assert
    # over samples: the stated values must be reachable, and must not be the
    # only reachable ones.
    styles = {_select_theme(intensity="medium", user_prefs=prefs)["style"] for _ in range(200)}

    assert "placeholder distinctive style" in styles
    assert len(styles) > 1, "a stated preference must bias selection, not lock it"


@pytest.mark.asyncio
async def test_maybe_reward_uses_stored_prefs_without_mock(clean_prefs: Any) -> None:
    """Stored prefs reach generate_reward_image through the real maybe_reward path.

    Proves end-to-end wiring: prefs in Postgres -> load_reward_prefs (real, not
    patched) -> maybe_reward -> generate_reward_image receives the profile.
    Outbound image generation and manifest write are mocked; DB reads are real.
    """
    import uuid

    from app.tools import rewards as rewards_module

    await _write_prefs(
        clean_prefs,
        json.dumps(
            {
                "rewards": {
                    "preferred_styles": ["placeholder e2e style"],
                    "preferred_palettes": ["placeholder e2e palette"],
                }
            }
        ),
    )

    image_mock = AsyncMock(
        return_value={
            "image": {
                "path": "/tmp/reward_artifacts/placeholder.png",
                "theme_family": "placeholder",
                "style": "placeholder e2e style",
                "palette": "placeholder e2e palette",
            },
            "failure_reason": None,
        }
    )

    with (
        patch.object(rewards_module, "generate_reward_image", new=image_mock),
        patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
        patch.object(rewards_module, "compute_intensity", return_value=("high", 70)),
    ):
        await rewards_module.maybe_reward(
            peer=_PEER,
            task_title="Placeholder task title",
            notion_page_id="<page-id-e2e>",
            streak=2,
            energy_required="High",
            time_estimate=45,
        )

    assert image_mock.called
    passed_prefs = image_mock.await_args.kwargs["user_prefs"]
    assert passed_prefs is not None
    assert passed_prefs.get("preferred_styles") == ["placeholder e2e style"]
    assert passed_prefs.get("preferred_palettes") == ["placeholder e2e palette"]
