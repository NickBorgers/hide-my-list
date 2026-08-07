"""Integration tests for load_user_prefs() and its reward-flow wiring.

Verifies against a real Postgres that:

  - load_user_prefs() reads the rewards subtree of prefs_json and round-trips
    correctly through psycopg's JSONB column.
  - maybe_reward() passes the stored prefs to generate_reward_image() when
    user_prefs is not supplied by the caller.

The unit tests mock get_db_conn(), so they can only prove the call site passes
values to the right parameter. These tests prove the column exists, psycopg
deserializes the JSONB correctly, and the end-to-end wiring actually reaches
generate_reward_image() — the failure mode a mocked connection cannot see.

Tests require a live DATABASE_URL. Skipped otherwise.

Private data discipline: all identifiers and content use placeholders; no real
page IDs, recipients, or task titles.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
pytestmark = pytest.mark.skipif(
    not _HAS_DB,
    reason="DATABASE_URL not set; skipping integration tests",
)

_PEER = "<test-peer-user-prefs>"


@pytest.fixture()
async def clean_user_prefs() -> Any:
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


@pytest.mark.asyncio
async def test_load_user_prefs_round_trips_rewards_json(clean_user_prefs: Any) -> None:
    """The rewards subtree round-trips through psycopg's JSONB column.

    Proves the column exists and psycopg deserializes JSON correctly — the
    failure mode a mocked connection cannot see.
    """
    from app.tools.rewards import load_user_prefs

    rewards_prefs = {
        "preferred_styles": ["test-style"],
        "preferred_palettes": ["test-palette"],
        "favorite_subjects": ["space"],
        "avoid": ["medical literal"],
        "humor_level": "subtle",
    }

    await clean_user_prefs.execute(
        "INSERT INTO user_prefs (peer, prefs_json) VALUES (%s, %s::jsonb)",
        (_PEER, json.dumps({"rewards": rewards_prefs})),
    )
    await clean_user_prefs.commit()

    result = await load_user_prefs(_PEER)
    assert result == rewards_prefs


@pytest.mark.asyncio
async def test_load_user_prefs_missing_row_returns_empty(clean_user_prefs: Any) -> None:
    """A peer with no user_prefs row returns an empty dict — no crash."""
    from app.tools.rewards import load_user_prefs

    result = await load_user_prefs("<test-peer-user-prefs-no-row>")
    assert result == {}


@pytest.mark.asyncio
async def test_maybe_reward_loads_stored_prefs_from_db(clean_user_prefs: Any) -> None:
    """maybe_reward() passes stored rewards prefs to generate_reward_image().

    Proves the end-to-end wiring: load_user_prefs() inside maybe_reward()
    reads from Postgres, and its return value reaches the image generation
    call — the path that was broken before prefs were loaded from Postgres.
    """
    from app.tools import rewards as rewards_module

    rewards_prefs = {
        "preferred_styles": ["integration-test-style"],
    }

    await clean_user_prefs.execute(
        "INSERT INTO user_prefs (peer, prefs_json) VALUES (%s, %s::jsonb)",
        (_PEER, json.dumps({"rewards": rewards_prefs})),
    )
    await clean_user_prefs.commit()

    captured_prefs: list[Any] = []

    async def _capture_generate(**kwargs: Any) -> Any:
        captured_prefs.append(kwargs.get("user_prefs"))
        return {"image": None, "failure_reason": "no_api_key"}

    with (
        patch.object(rewards_module, "generate_reward_image", side_effect=_capture_generate),
        patch.object(rewards_module, "classify_task_motif", new=AsyncMock(return_value="errand")),
        patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
    ):
        await rewards_module.maybe_reward(
            peer=_PEER,
            task_title="Placeholder task title",
            notion_page_id="<page-tst-001>",
            streak=1,
            energy_required="Medium",
            time_estimate=30,
        )

    assert len(captured_prefs) == 1, "generate_reward_image must be called once"
    assert captured_prefs[0] == rewards_prefs, (
        "stored rewards prefs must reach generate_reward_image"
    )
