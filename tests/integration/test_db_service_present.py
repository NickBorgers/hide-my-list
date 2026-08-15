"""Positive control for the `pytest-db` CI job.

Every DB-backed test in this directory guards itself with
`skipif(not os.environ.get("DATABASE_URL"))`. That is right for a laptop with no
Postgres running, but it means a CI job whose service container failed to start
reports a green required check having executed nothing — the exact false-green
the job exists to prevent.

This module does not skip under CI. If `CI=true` and the database is missing or
unreachable, it fails loudly.
"""
from __future__ import annotations

import os

import pytest

_IN_CI = os.environ.get("CI", "").lower() in ("true", "1", "yes")
_DATABASE_URL = os.environ.get("DATABASE_URL", "")


def test_ci_provides_a_database_url() -> None:
    if not _IN_CI:
        pytest.skip("not running under CI; DB-backed tests may legitimately skip")

    assert _DATABASE_URL, (
        "CI=true but DATABASE_URL is unset. The pytest-db job's Postgres service "
        "is missing, so every DB-backed test would skip and the required check "
        "would report green having verified nothing."
    )


@pytest.mark.skipif(not _DATABASE_URL, reason="DATABASE_URL not set")
async def test_database_is_reachable_and_migrated() -> None:
    """Connect for real and confirm the migration runner's schema is present.

    Asserting on a table rather than `SELECT 1` also covers the case where the
    container is up but migrations never ran.
    """
    import psycopg

    from app.tools.db import run_migrations

    run_migrations()  # synchronous by design — called at startup before the loop

    async with await psycopg.AsyncConnection.connect(_DATABASE_URL) as conn:
        cursor = await conn.execute("SELECT to_regclass('reminder_outbox')")
        row = await cursor.fetchone()

    assert row is not None and row[0] == "reminder_outbox", (
        "reminder_outbox is absent after run_migrations() — the database is "
        "reachable but unmigrated."
    )
