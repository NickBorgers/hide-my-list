"""Integration tests for migration 0007: reminder_scheduling_ledger schema.

Verifies:
  - Round-trip insert + query of a ledger row preserves all fields.
  - Multiple ledger rows for the same notion_page_id succeed (no UNIQUE on page_id).
  - FK violation on nonexistent reminder_outbox_id is rejected.
  - Superseded rows are excluded from the rsl_assigned_slot partial index
    (verified via pg_indexes definition check).
  - Multiple outbox rows for the same notion_page_id succeed after UNIQUE drop.

Tests require a live DATABASE_URL. Skipped otherwise.

Private data discipline: all identifiers and content use placeholders; no real
page IDs, recipients, or appointment titles.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
pytestmark = pytest.mark.skipif(
    not _HAS_DB,
    reason="DATABASE_URL not set; skipping integration tests",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db_conn() -> Any:
    """Provide a psycopg connection with all migrations applied and tables clean."""
    import psycopg

    conn_str = os.environ["DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(conn_str, autocommit=False) as conn:
        # Apply all migrations in order (idempotent)
        from app.tools.db import _MIGRATIONS_DIR

        for mig in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await conn.execute(mig.read_text())  # type: ignore[arg-type]
        await conn.commit()

        # Clean state: truncate dependent tables in dependency order
        await conn.execute(
            "TRUNCATE reminder_scheduling_ledger, deadline_task_peers, reminder_outbox, "
            "recent_outbound, ops_alerts_throttle"
        )
        await conn.commit()

        yield conn


def _make_outbox_row(
    notion_page_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Build a minimal reminder_outbox insert payload."""
    return {
        "id": str(uuid.uuid4()),
        "notion_page_id": notion_page_id or f"page-{uuid.uuid4()}",
        "peer": "<recipient>",
        "body": "Test reminder body",
        "due_at": datetime.now(UTC) + timedelta(days=1),
        "state": "pending",
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
    }


async def _insert_outbox(conn: Any, **overrides: Any) -> str:
    """Insert one reminder_outbox row and return its id."""
    row = _make_outbox_row(**overrides)
    await conn.execute(
        """
        INSERT INTO reminder_outbox
          (id, notion_page_id, peer, body, due_at, state, idempotency_key)
        VALUES
          (%(id)s, %(notion_page_id)s, %(peer)s, %(body)s, %(due_at)s,
           %(state)s, %(idempotency_key)s)
        """,
        row,
    )
    return row["id"]


async def _insert_ledger(conn: Any, outbox_id: str, **overrides: Any) -> str:
    """Insert one reminder_scheduling_ledger row and return its id."""
    now = datetime.now(UTC)
    defaults: dict[str, Any] = {
        "notion_page_id": f"page-{uuid.uuid4()}",
        "deadline_at": now + timedelta(days=7),
        "urgency": 50,
        "tier": "standard",
        "milestone_label": "3d",
        "ideal_slot_at": now + timedelta(days=4),
        "assigned_slot_at": now + timedelta(days=4),
        "reminder_outbox_id": outbox_id,
        "superseded_at": None,
    }
    defaults.update(overrides)

    row_id = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO reminder_scheduling_ledger
          (id, notion_page_id, deadline_at, urgency, tier, milestone_label,
           ideal_slot_at, assigned_slot_at, reminder_outbox_id, superseded_at)
        VALUES
          (%(id)s, %(notion_page_id)s, %(deadline_at)s, %(urgency)s, %(tier)s,
           %(milestone_label)s, %(ideal_slot_at)s, %(assigned_slot_at)s,
           %(reminder_outbox_id)s, %(superseded_at)s)
        """,
        {**defaults, "id": row_id},
    )
    return row_id


# ---------------------------------------------------------------------------
# Test 1: Round-trip — insert a row, query it back, assert all fields preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ledger_round_trip(db_conn: Any) -> None:
    """Insert a ledger row and query it back; all fields must match."""
    import psycopg.rows

    outbox_id = await _insert_outbox(db_conn)
    await db_conn.commit()

    now = datetime.now(UTC)
    page_id = f"page-{uuid.uuid4()}"
    deadline = now + timedelta(days=14)
    ideal = now + timedelta(days=11)
    assigned = now + timedelta(days=11, hours=1)

    ledger_id = await _insert_ledger(
        db_conn,
        outbox_id,
        notion_page_id=page_id,
        deadline_at=deadline,
        urgency=75,
        tier="dense",
        milestone_label="14d",
        ideal_slot_at=ideal,
        assigned_slot_at=assigned,
    )
    await db_conn.commit()

    async with db_conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            "SELECT * FROM reminder_scheduling_ledger WHERE id = %s",
            (ledger_id,),
        )
        row = await cur.fetchone()

    assert row is not None, "Ledger row not found after insert"
    assert str(row["id"]) == ledger_id
    assert row["notion_page_id"] == page_id
    assert row["urgency"] == 75
    assert row["tier"] == "dense"
    assert row["milestone_label"] == "14d"
    assert str(row["reminder_outbox_id"]) == outbox_id
    assert row["superseded_at"] is None
    # scheduled_at populated by DEFAULT now()
    assert row["scheduled_at"] is not None


# ---------------------------------------------------------------------------
# Test 2: Same notion_page_id, different deadline — must succeed (no UNIQUE)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_ledger_rows_same_page_id(db_conn: Any) -> None:
    """Two ledger rows for the same notion_page_id with different deadlines must succeed."""
    now = datetime.now(UTC)
    page_id = f"page-{uuid.uuid4()}"

    # Two separate outbox rows (one per reminder)
    outbox_id_a = await _insert_outbox(db_conn)
    outbox_id_b = await _insert_outbox(db_conn)
    await db_conn.commit()

    await _insert_ledger(
        db_conn,
        outbox_id_a,
        notion_page_id=page_id,
        deadline_at=now + timedelta(days=7),
        milestone_label="3d",
    )
    await _insert_ledger(
        db_conn,
        outbox_id_b,
        notion_page_id=page_id,
        deadline_at=now + timedelta(days=14),  # different deadline
        milestone_label="7d",
    )
    await db_conn.commit()

    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM reminder_scheduling_ledger WHERE notion_page_id = %s",
            (page_id,),
        )
        count = (await cur.fetchone())[0]

    assert count == 2, f"Expected 2 ledger rows for same page_id, got {count}"


# ---------------------------------------------------------------------------
# Test 3: FK violation — nonexistent reminder_outbox_id must fail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ledger_fk_violation_on_missing_outbox(db_conn: Any) -> None:
    """Inserting a ledger row with a nonexistent outbox FK must raise an error."""
    import psycopg

    nonexistent_outbox_id = str(uuid.uuid4())

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await _insert_ledger(db_conn, nonexistent_outbox_id)
        await db_conn.commit()

    await db_conn.rollback()


# ---------------------------------------------------------------------------
# Test 4: Superseded rows excluded from partial index definition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_index_excludes_superseded(db_conn: Any) -> None:
    """The rsl_assigned_slot partial index definition must include WHERE superseded_at IS NULL.

    Verified by checking pg_indexes rather than query plans, which avoids
    planner-version sensitivity while still asserting the constraint is wired.
    """
    import psycopg.rows

    async with db_conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        await cur.execute(
            """
            SELECT indexdef
              FROM pg_indexes
             WHERE tablename = 'reminder_scheduling_ledger'
               AND indexname = 'rsl_assigned_slot'
            """
        )
        row = await cur.fetchone()

    assert row is not None, "Index rsl_assigned_slot not found in pg_indexes"
    index_def = row["indexdef"].lower()
    assert "superseded_at is null" in index_def, (
        f"Expected 'superseded_at is null' in index definition, got: {index_def}"
    )


# ---------------------------------------------------------------------------
# Test 5: Multiple outbox rows same notion_page_id — UNIQUE constraint dropped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox_allows_multiple_rows_same_page_id(db_conn: Any) -> None:
    """After migration 0007 drops the UNIQUE on reminder_outbox.notion_page_id,
    inserting two outbox rows for the same page must succeed."""
    page_id = f"page-{uuid.uuid4()}"

    await _insert_outbox(db_conn, notion_page_id=page_id, idempotency_key=str(uuid.uuid4()))
    await _insert_outbox(db_conn, notion_page_id=page_id, idempotency_key=str(uuid.uuid4()))
    await db_conn.commit()

    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM reminder_outbox WHERE notion_page_id = %s",
            (page_id,),
        )
        count = (await cur.fetchone())[0]

    assert count == 2, (
        f"Expected 2 outbox rows for same notion_page_id after UNIQUE drop, got {count}"
    )


# ---------------------------------------------------------------------------
# Test 6: idempotency_key UNIQUE — duplicate key must fail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox_idempotency_key_unique(db_conn: Any) -> None:
    """After migration 0007 adds UNIQUE on reminder_outbox.idempotency_key,
    inserting two rows with the same idempotency_key must raise UniqueViolation,
    even when notion_page_id and due_at differ (proving it's the key enforcing dedup)."""
    import psycopg

    shared_key = "dedup-test-1"

    # First insert — different page_id and due_at from the second
    await _insert_outbox(
        db_conn,
        notion_page_id=f"page-{uuid.uuid4()}",
        idempotency_key=shared_key,
    )
    await db_conn.commit()

    # Second insert — same idempotency_key, distinct page_id and due_at
    with pytest.raises(psycopg.errors.UniqueViolation):
        await _insert_outbox(
            db_conn,
            notion_page_id=f"page-{uuid.uuid4()}",
            idempotency_key=shared_key,
        )
        await db_conn.commit()

    await db_conn.rollback()


@pytest.mark.asyncio
async def test_outbox_kind_default_and_check_constraint(db_conn: Any) -> None:
    """Migration 0008 adds reminder/deadline discriminator with safe default."""
    import psycopg

    outbox_id = await _insert_outbox(db_conn)
    await db_conn.commit()

    async with db_conn.cursor() as cur:
        await cur.execute("SELECT kind FROM reminder_outbox WHERE id = %s", (outbox_id,))
        row = await cur.fetchone()

    assert row[0] == "reminder"

    with pytest.raises(psycopg.errors.CheckViolation):
        await db_conn.execute(
            """
            INSERT INTO reminder_outbox
              (id, notion_page_id, peer, body, due_at, state, idempotency_key, kind)
            VALUES (%s, %s, %s, %s, %s, 'pending', %s, 'invalid')
            """,
            (
                str(uuid.uuid4()),
                f"page-{uuid.uuid4()}",
                "<recipient>",
                "Test reminder body",
                datetime.now(UTC) + timedelta(days=1),
                str(uuid.uuid4()),
            ),
        )
        await db_conn.commit()

    await db_conn.rollback()
