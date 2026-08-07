"""Integration tests for migration 0012: per-peer reward descriptor vocabulary.

The vocabulary moved from process constants into Postgres so it can grow and
retire per peer. That introduces failure modes a mocked connection cannot show:
the CHECK constraints, the COALESCE-based unique index that has to make seeding
idempotent under concurrency, the partial index the active-row query relies on,
and the requirement that an unavailable database costs the user nothing.

Tests require a live DATABASE_URL. Skipped otherwise.

Private data discipline: all identifiers are placeholders; descriptor values
are the repo's own seed constants or explicit placeholder text.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import pytest

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
pytestmark = pytest.mark.skipif(
    not _HAS_DB,
    reason="DATABASE_URL not set; skipping integration tests",
)

_PEER = "<test-peer-theme-pool>"


@pytest.fixture()
async def clean_pool() -> Any:
    """Apply migrations and clear reward_theme_pool for this test's peer."""
    import psycopg

    from app.tools import reward_pool

    conn_str = os.environ["DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(conn_str, autocommit=False) as conn:
        from app.tools.db import _MIGRATIONS_DIR

        for mig in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await conn.execute(mig.read_text())  # type: ignore[arg-type]
        await conn.commit()

        await conn.execute("DELETE FROM reward_theme_pool WHERE peer = %s", (_PEER,))
        await conn.commit()
        # The process-level memo would otherwise hide a wiped table.
        reward_pool._seeded_peers.discard(_PEER)
        yield conn

        await conn.execute("DELETE FROM reward_theme_pool WHERE peer = %s", (_PEER,))
        await conn.commit()
        reward_pool._seeded_peers.discard(_PEER)


async def _count(conn: Any) -> int:
    cur = await conn.execute(
        "SELECT count(*) AS n FROM reward_theme_pool WHERE peer = %s", (_PEER,)
    )
    row = await cur.fetchone()
    return int(row[0])


@pytest.mark.asyncio
async def test_seeding_populates_every_axis(clean_pool: Any) -> None:
    """The seed constants land in the table, scoped as the schema requires."""
    from app.tools.reward_pool import ensure_seeded
    from app.tools.rewards import _SEED_PALETTES, _SEED_STYLES, _SEED_THEMES

    assert await ensure_seeded(_PEER) is True

    expected = (
        sum(len(v) for v in _SEED_THEMES.values()) + len(_SEED_STYLES) + len(_SEED_PALETTES)
    )
    assert await _count(clean_pool) == expected

    # Themes carry an intensity; style and palette must not, so their feedback
    # pools across intensities.
    cur = await clean_pool.execute(
        """
        SELECT axis, count(*) FILTER (WHERE intensity IS NULL) AS null_intensity
        FROM reward_theme_pool WHERE peer = %s GROUP BY axis ORDER BY axis
        """,
        (_PEER,),
    )
    by_axis = {row[0]: row[1] for row in await cur.fetchall()}
    assert by_axis["theme"] == 0
    assert by_axis["style"] == len(_SEED_STYLES)
    assert by_axis["palette"] == len(_SEED_PALETTES)


@pytest.mark.asyncio
async def test_seeding_is_idempotent(clean_pool: Any) -> None:
    """Re-seeding an already-seeded peer inserts nothing new."""
    from app.tools import reward_pool

    await reward_pool.ensure_seeded(_PEER)
    first = await _count(clean_pool)

    reward_pool._seeded_peers.discard(_PEER)  # force a real round trip
    await reward_pool.ensure_seeded(_PEER)

    assert await _count(clean_pool) == first


@pytest.mark.asyncio
async def test_concurrent_seeding_does_not_duplicate(clean_pool: Any) -> None:
    """Two processes racing to seed the same peer both succeed, once.

    The unique index has to COALESCE intensity, or NULL-intensity style and
    palette rows would compare unequal to themselves and duplicate.
    """
    from app.tools import reward_pool
    from app.tools.rewards import _SEED_PALETTES, _SEED_STYLES, _SEED_THEMES

    async def seed() -> bool:
        reward_pool._seeded_peers.discard(_PEER)
        return await reward_pool.ensure_seeded(_PEER)

    results = await asyncio.gather(seed(), seed(), seed())

    assert all(results)
    expected = (
        sum(len(v) for v in _SEED_THEMES.values()) + len(_SEED_STYLES) + len(_SEED_PALETTES)
    )
    assert await _count(clean_pool) == expected


@pytest.mark.asyncio
async def test_theme_rows_are_scoped_to_their_intensity(clean_pool: Any) -> None:
    """Loading one intensity returns that intensity's themes and no others."""
    from app.tools.reward_pool import load_vocabulary
    from app.tools.rewards import _SEED_PALETTES, _SEED_STYLES, _SEED_THEMES

    vocabulary = await load_vocabulary(_PEER, intensity="high")

    assert vocabulary is not None
    assert set(vocabulary["theme"]) == set(_SEED_THEMES["high"])
    assert set(vocabulary["style"]) == set(_SEED_STYLES)
    assert set(vocabulary["palette"]) == set(_SEED_PALETTES)


@pytest.mark.asyncio
async def test_retired_rows_never_reach_selection(clean_pool: Any) -> None:
    """Retirement is a soft delete that removes a value from the draw."""
    from app.tools.reward_pool import load_vocabulary
    from app.tools.rewards import _SEED_STYLES

    retired = _SEED_STYLES[0]
    await load_vocabulary(_PEER, intensity="high")  # seed
    await clean_pool.execute(
        """
        UPDATE reward_theme_pool SET retired_at = now()
        WHERE peer = %s AND axis = 'style' AND value = %s
        """,
        (_PEER, retired),
    )
    await clean_pool.commit()

    vocabulary = await load_vocabulary(_PEER, intensity="high")

    assert vocabulary is not None
    assert retired not in vocabulary["style"]
    # The row survives so its feedback attribution is not lost.
    cur = await clean_pool.execute(
        "SELECT count(*) FROM reward_theme_pool WHERE peer = %s AND value = %s",
        (_PEER, retired),
    )
    assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_incomplete_vocabulary_falls_back_rather_than_narrowing(
    clean_pool: Any,
) -> None:
    """An empty axis yields None, not a partial vocabulary.

    A half-loaded axis would silently collapse selection onto whatever
    remained — the exact failure the whole subsystem exists to prevent.
    """
    from app.tools.reward_pool import load_vocabulary

    await load_vocabulary(_PEER, intensity="high")  # seed
    await clean_pool.execute(
        "UPDATE reward_theme_pool SET retired_at = now() WHERE peer = %s AND axis = 'style'",
        (_PEER,),
    )
    await clean_pool.commit()

    assert await load_vocabulary(_PEER, intensity="high") is None


@pytest.mark.asyncio
async def test_record_use_increments_the_drawn_descriptors(clean_pool: Any) -> None:
    """Usage counters land on the rows that were actually drawn."""
    from app.tools.reward_pool import load_vocabulary, record_use
    from app.tools.rewards import _SEED_PALETTES, _SEED_STYLES, _SEED_THEMES

    await load_vocabulary(_PEER, intensity="high")

    selection = {
        "theme_family": _SEED_THEMES["high"][0],
        "style": _SEED_STYLES[0],
        "palette": _SEED_PALETTES[0],
    }
    await record_use(_PEER, selection=selection, intensity="high")

    cur = await clean_pool.execute(
        """
        SELECT value, use_count, last_used_at
        FROM reward_theme_pool
        WHERE peer = %s AND use_count > 0 ORDER BY value
        """,
        (_PEER,),
    )
    rows = await cur.fetchall()

    assert {row[0] for row in rows} == set(selection.values())
    assert all(row[1] == 1 for row in rows)
    assert all(row[2] is not None for row in rows)


@pytest.mark.asyncio
async def test_intensity_check_constraint_is_enforced(clean_pool: Any) -> None:
    """The schema, not just the writer, keeps axes scoped correctly."""
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        await clean_pool.execute(
            """
            INSERT INTO reward_theme_pool (id, peer, axis, intensity, value, origin)
            VALUES (%s, %s, 'style', 'high', 'placeholder style', 'seed')
            """,
            (uuid.uuid4(), _PEER),
        )
    await clean_pool.rollback()

    with pytest.raises(psycopg.errors.CheckViolation):
        await clean_pool.execute(
            """
            INSERT INTO reward_theme_pool (id, peer, axis, intensity, value, origin)
            VALUES (%s, %s, 'theme', NULL, 'placeholder theme', 'seed')
            """,
            (uuid.uuid4(), _PEER),
        )
    await clean_pool.rollback()


_PEER_FLOW = "<test-peer-theme-pool-flow>"


@pytest.fixture()
async def clean_pool_flow() -> Any:
    """Apply migrations and clear reward_theme_pool for the flow reachability test."""
    import psycopg

    from app.tools import reward_pool

    conn_str = os.environ["DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(conn_str, autocommit=False) as conn:
        from app.tools.db import _MIGRATIONS_DIR

        for mig in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await conn.execute(mig.read_text())  # type: ignore[arg-type]
        await conn.commit()

        await conn.execute("DELETE FROM reward_theme_pool WHERE peer = %s", (_PEER_FLOW,))
        await conn.commit()
        reward_pool._seeded_peers.discard(_PEER_FLOW)
        yield conn

        await conn.execute("DELETE FROM reward_theme_pool WHERE peer = %s", (_PEER_FLOW,))
        await conn.commit()
        reward_pool._seeded_peers.discard(_PEER_FLOW)


@pytest.mark.asyncio
async def test_maybe_reward_seeds_and_records_use_in_db(clean_pool_flow: Any) -> None:
    """ensure_seeded, load_vocabulary, and record_use are reachable from maybe_reward.

    Clause 1: new public app/tools functions must have an integration test asserting
    reachability from an end-to-end flow rather than direct calls only.
    """
    import uuid
    from unittest.mock import AsyncMock, patch

    from app.tools import rewards as rewards_module
    from app.tools.rewards import _SEED_PALETTES, _SEED_STYLES, _SEED_THEMES

    fake_image = {
        "path": "/tmp/reward_artifacts/test-flow.png",
        "theme_family": _SEED_THEMES["low"][0],
        "style": _SEED_STYLES[0],
        "palette": _SEED_PALETTES[0],
    }

    with (
        patch.object(rewards_module, "generate_reward_image", AsyncMock(return_value=fake_image)),
        patch.object(rewards_module, "write_reward_manifest", AsyncMock(return_value=uuid.uuid4())),
    ):
        result = await rewards_module.maybe_reward(
            peer=_PEER_FLOW,
            task_title="Placeholder test task",
            notion_page_id="<page-id-flow>",
            streak=1,
            energy_required="Low",
            time_estimate=15,
        )

    assert result["text"]

    # ensure_seeded was reached: reward_theme_pool has rows for this peer
    cur = await clean_pool_flow.execute(
        "SELECT count(*) FROM reward_theme_pool WHERE peer = %s", (_PEER_FLOW,)
    )
    row = await cur.fetchone()
    assert int(row[0]) > 0, "ensure_seeded was not called — reward_theme_pool is empty"

    # record_use was reached: at least one descriptor's use_count was incremented
    cur = await clean_pool_flow.execute(
        "SELECT count(*) FROM reward_theme_pool WHERE peer = %s AND use_count > 0",
        (_PEER_FLOW,),
    )
    row = await cur.fetchone()
    assert int(row[0]) > 0, "record_use was not called — no use_count incremented"
