"""Integration tests for APScheduler + orphan reconciliation (PR-A5).

Tests run against a real Postgres database when DATABASE_URL is set.
Skipped otherwise.

Orphan removal: insert a phantom job directly into apscheduler_jobs,
restart the scheduler, verify the phantom is removed and declared jobs
are present.
"""
from __future__ import annotations

import os

import pytest

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
pytestmark = pytest.mark.skipif(
    not _HAS_DB,
    reason="DATABASE_URL not set; skipping integration tests",
)


@pytest.fixture()
async def clean_scheduler_db() -> None:
    """Drop apscheduler_jobs table to ensure a clean slate."""
    import psycopg

    conn_str = os.environ["DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(conn_str, autocommit=True) as conn:
        await conn.execute("DROP TABLE IF EXISTS apscheduler_jobs")
    yield
    # Cleanup after test too
    async with await psycopg.AsyncConnection.connect(conn_str, autocommit=True) as conn:
        await conn.execute("DROP TABLE IF EXISTS apscheduler_jobs")


@pytest.mark.asyncio
async def test_orphan_job_removed_on_reconcile(clean_scheduler_db: None) -> None:
    """Phantom job in apscheduler_jobs is removed after reconcile_jobstore."""
    from app.scheduler.jobs import SCHEDULED_JOBS
    from app.scheduler.scheduler import build_scheduler

    # Build and start scheduler — this creates the table and registers declared jobs
    scheduler = await build_scheduler()
    scheduler.start()

    try:
        # Insert a phantom job directly into the jobstore DB
        # APScheduler's SQLAlchemy jobstore uses a 'apscheduler_jobs' table.
        # We insert a minimal row to simulate an orphan.
        import psycopg

        conn_str = os.environ["DATABASE_URL"]
        async with await psycopg.AsyncConnection.connect(conn_str, autocommit=True) as conn:
            await conn.execute(
                """
                INSERT INTO apscheduler_jobs (id, next_run_time, job_state)
                VALUES ('phantom_orphan_job', '2099-01-01 00:00:00.000000', %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (b"phantom",),
            )

        # Note: the phantom may not show up via get_jobs() until scheduler re-reads
        # from store. Call reconcile explicitly.

        from app.scheduler.jobs import reconcile_jobstore
        reconcile_jobstore(scheduler)

        # Phantom should be gone; declared jobs should be present
        final_ids = {j.id for j in scheduler.get_jobs()}
        declared_ids = {j.id for j in SCHEDULED_JOBS}

        assert "phantom_orphan_job" not in final_ids, "Orphan job was not removed"
        assert declared_ids <= final_ids, (
            f"Missing declared jobs: {declared_ids - final_ids}"
        )

    finally:
        scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_declared_jobs_present_after_scheduler_start(clean_scheduler_db: None) -> None:
    """All SCHEDULED_JOBS are registered after scheduler.start() + reconcile."""
    from app.scheduler.jobs import SCHEDULED_JOBS
    from app.scheduler.scheduler import build_scheduler

    scheduler = await build_scheduler()
    scheduler.start()

    try:
        job_ids = {j.id for j in scheduler.get_jobs()}
        declared_ids = {j.id for j in SCHEDULED_JOBS}
        assert declared_ids <= job_ids, f"Missing: {declared_ids - job_ids}"
    finally:
        scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_reminder_dispatcher_job_invokes_worker(clean_scheduler_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """reminder_dispatcher job calls reminder_worker.run_worker_once."""
    import app.scheduler.reminder_worker as worker_module
    from app.scheduler.jobs import dispatch_due_reminders

    called = False

    async def mock_run_worker_once(**_: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(worker_module, "run_worker_once", mock_run_worker_once)

    # Invoke the job function directly (not via scheduler timer)
    await dispatch_due_reminders()

    assert called, "reminder_worker.run_worker_once was not called by reminder_dispatcher job"
