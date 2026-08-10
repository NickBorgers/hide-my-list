"""Per-peer reward descriptor vocabulary (migration 0012).

Theme, style, and palette vocabularies live in `reward_theme_pool` so they can
grow and retire per peer over time. The constants in app/tools/rewards.py stay
the seed set and the fallback: every function here degrades to them rather than
failing, because a vocabulary lookup must never cost the user a reward.

Sensitive-task rewards deliberately never call into this module. Their
allowlist stays a code constant so that path cannot be steered by stored
content — a structural guarantee rather than a filter.

Private data discipline: peer is a DB filter key only. Descriptor values are
sanitized preference text and must never be written to a log.
"""
from __future__ import annotations

import uuid

import structlog

log = structlog.get_logger(__name__)

# Peers already seeded in this process. Seeding is idempotent at the database
# level, so this is purely to keep the first reward of every later completion
# from re-issuing ~36 no-op INSERTs.
_seeded_peers: set[str] = set()


def _seed_rows(peer: str) -> list[tuple[uuid.UUID, str, str, str | None, str, str]]:
    """Build the INSERT payload for a peer's seed vocabulary."""
    from app.tools.rewards import _SEED_PALETTES, _SEED_STYLES, _SEED_THEMES

    rows: list[tuple[uuid.UUID, str, str, str | None, str, str]] = []
    for intensity, themes in _SEED_THEMES.items():
        for value in themes:
            rows.append((uuid.uuid4(), peer, "theme", intensity, value, "seed"))
    for value in _SEED_STYLES:
        rows.append((uuid.uuid4(), peer, "style", None, value, "seed"))
    for value in _SEED_PALETTES:
        rows.append((uuid.uuid4(), peer, "palette", None, value, "seed"))
    return rows


async def ensure_seeded(peer: str) -> bool:
    """Insert this peer's seed vocabulary if it is not already present.

    Idempotent at the database level via the unique index, so concurrent
    callers are safe and a partially-seeded peer is completed rather than
    duplicated. Returns True when the vocabulary is known to be present.

    Returns False on any failure, which callers read as "fall back to the seed
    constants". Seeding is an optimization, not a precondition.
    """
    if peer in _seeded_peers:
        return True

    from app.tools.db import get_db_conn

    try:
        rows = _seed_rows(peer)
        async with get_db_conn() as conn:
            for row in rows:
                await conn.execute(
                    """
                    INSERT INTO reward_theme_pool
                        (id, peer, axis, intensity, value, origin)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (peer, axis, COALESCE(intensity, ''), value)
                    DO NOTHING
                    """,
                    row,
                )
        _seeded_peers.add(peer)
        log.info("reward_theme_pool.seeded", count=len(rows))
        return True

    except Exception:
        log.warning("reward_theme_pool.seed_failed")
        return False


async def load_vocabulary(peer: str, *, intensity: str) -> dict[str, list[str]] | None:
    """Load this peer's active descriptors for one intensity.

    Returns a dict with 'theme' / 'style' / 'palette' keys, or None when the
    vocabulary is unavailable or incomplete — in which case the caller uses the
    seed constants. Returning None rather than a partial vocabulary is
    deliberate: a half-loaded axis would silently narrow selection, which is
    the failure this whole subsystem exists to avoid.

    Retired rows are excluded. Ordering is stable so that selection weights line
    up with the vocabulary across calls.
    """
    if not await ensure_seeded(peer):
        return None

    from app.tools.db import get_db_conn

    try:
        async with get_db_conn() as conn:
            cur = await conn.execute(
                """
                SELECT axis, value
                FROM reward_theme_pool
                WHERE peer = %s
                  AND retired_at IS NULL
                  AND (axis <> 'theme' OR intensity = %s)
                ORDER BY axis, value
                """,
                (peer, intensity),
            )
            rows = await cur.fetchall()

        vocabulary: dict[str, list[str]] = {"theme": [], "style": [], "palette": []}
        for row in rows:
            axis = row["axis"]
            if axis in vocabulary:
                vocabulary[axis].append(row["value"])

        if not all(vocabulary.values()):
            # An empty axis would collapse selection to whatever remains.
            log.warning("reward_theme_pool.incomplete_vocabulary")
            return None

        return vocabulary

    except Exception:
        log.warning("reward_theme_pool.load_failed")
        return None


async def record_use(peer: str, *, selection: dict[str, str], intensity: str) -> None:
    """Bump usage counters for the descriptors just drawn.

    Best effort and deliberately swallowing: usage statistics are diagnostic,
    and a counter update must never fail a reward that has already been chosen.
    """
    from app.tools.db import get_db_conn

    targets = (
        ("theme", intensity, selection.get("theme_family", "")),
        ("style", None, selection.get("style", "")),
        ("palette", None, selection.get("palette", "")),
    )

    try:
        async with get_db_conn() as conn:
            for axis, axis_intensity, value in targets:
                if not value:
                    continue
                await conn.execute(
                    """
                    UPDATE reward_theme_pool
                    SET use_count = use_count + 1,
                        last_used_at = now()
                    WHERE peer = %s
                      AND axis = %s
                      AND COALESCE(intensity, '') = COALESCE(%s, '')
                      AND value = %s
                    """,
                    (peer, axis, axis_intensity, value),
                )
    except Exception:
        log.debug("reward_theme_pool.record_use_failed")
