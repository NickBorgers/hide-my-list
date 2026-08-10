"""Database helpers for hide-my-list.

Provides connection management and migration runner.
"""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import psycopg.rows
import structlog

log = structlog.get_logger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations"


def get_connection_string() -> str:
    return os.environ["DATABASE_URL"]


@asynccontextmanager
async def get_db_conn() -> AsyncGenerator[psycopg.AsyncConnection[Any], None]:
    """Async context manager for a short-lived psycopg connection.

    Usage:
        async with get_db_conn() as conn:
            await conn.execute(...)
    """
    conn_str = get_connection_string()
    async with await psycopg.AsyncConnection.connect(
        conn_str, row_factory=psycopg.rows.dict_row
    ) as conn:
        yield conn


def run_migrations() -> None:
    """Run all SQL migration files in order against the configured database.

    Called at app startup before any other DB operations.
    Files are applied in filename order; each is idempotent (IF NOT EXISTS).
    """
    conn_str = get_connection_string()
    migration_files = sorted(_MIGRATIONS_DIR.glob("*.sql"))

    if not migration_files:
        log.info("db.migrations.none_found")
        return

    with psycopg.connect(conn_str) as conn:
        for mig_file in migration_files:
            log.info("db.migration.applying", file=mig_file.name)
            sql = mig_file.read_text(encoding="utf-8")
            conn.execute(sql)
            conn.commit()
            log.info("db.migration.done", file=mig_file.name)
