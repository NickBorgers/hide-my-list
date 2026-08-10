"""APScheduler v3 wiring with PostgresJobStore.

Uses AsyncIOScheduler so it integrates cleanly with the asyncio event loop.
The Postgres jobstore ensures job state survives restarts (same DB as outbox).
"""
from __future__ import annotations

import os
from typing import Any

import structlog
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.scheduler.jobs import reconcile_jobstore

log = structlog.get_logger(__name__)


async def build_scheduler(
    *,
    database_url: str | None = None,
    skip_reconcile: bool = False,
) -> AsyncIOScheduler:
    """Build an AsyncIOScheduler with Postgres jobstore and reconciled job list.

    Args:
        database_url: Override DATABASE_URL (used in tests).
        skip_reconcile: Skip reconcile_jobstore call (used in tests that
            check jobstore state directly).

    Returns:
        A configured (but not yet started) AsyncIOScheduler.
    """
    db_url = database_url or os.environ.get("DATABASE_URL", "")

    jobstores: dict[str, Any] = {}
    if db_url:
        # Convert asyncpg/psycopg URL to SQLAlchemy-compatible format for APScheduler.
        # APScheduler v3 jobstore uses SQLAlchemy sync engine under the hood.
        sa_url = db_url.replace("postgresql+psycopg://", "postgresql://")
        sa_url = sa_url.replace("postgresql+asyncpg://", "postgresql://")
        jobstores["default"] = SQLAlchemyJobStore(url=sa_url)
    else:
        log.warning("scheduler.no_db_url.using_memory_jobstore")

    executors: dict[str, Any] = {
        "default": AsyncIOExecutor(),
    }

    scheduler = AsyncIOScheduler(
        jobstores=jobstores,
        executors=executors,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 300,
        },
    )

    if not skip_reconcile:
        reconcile_jobstore(scheduler)

    return scheduler
