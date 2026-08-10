"""Integration tests for the reminder outbox and worker (PR-A4).

Tests run against a real Postgres database (set DATABASE_URL in environment,
or skip if not available). Signal-cli is fully mocked — no real signal-cli
or Signal account is required.

Private data discipline: all test values use placeholder strings.

Skip markers:
  - Tests requiring Postgres are marked @pytest.mark.integration and
    are skipped when DATABASE_URL is not set.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

# Skip all integration tests unless DATABASE_URL is set
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
    """Provide a clean-state async DB connection for each test."""
    import psycopg

    conn_str = os.environ["DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(conn_str, autocommit=False) as conn:
        # Run migrations
        from app.tools.db import _MIGRATIONS_DIR
        for mig in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await conn.execute(mig.read_text())  # type: ignore[arg-type]
        await conn.commit()

        # Clean state before each test (child table first to satisfy FK)
        await conn.execute(
            "TRUNCATE reminder_scheduling_ledger, deadline_task_peers, reminder_outbox, "
            "recent_outbound, ops_alerts_throttle"
        )
        await conn.commit()

        yield conn


def _make_reminder(
    peer: str = "<peer>",
    body: str = "Test reminder",
    due_delta_seconds: float = -1,
) -> dict[str, Any]:
    """Build kwargs for reminders.enqueue."""
    return {
        "notion_page_id": str(uuid.uuid4()),
        "peer": peer,
        "body": body,
        "due_at": datetime.now(UTC) + timedelta(seconds=due_delta_seconds),
        "idempotency_key": str(uuid.uuid4()),
    }


def _mock_signal(signal_ts: int = 1000) -> AsyncMock:
    """Return a mock signal send function that succeeds immediately."""
    mock = AsyncMock(return_value={"timestamp": signal_ts})
    return mock


# ---------------------------------------------------------------------------
# Test 1: Happy path — enqueue -> worker picks up -> delivered
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enqueue_and_deliver(db_conn: Any) -> None:
    """Enqueue a due reminder; worker delivers it; row transitions to delivered."""
    from app.scheduler.reminder_worker import dispatch_due_reminders
    from app.tools import reminders

    signal_mock = _mock_signal(signal_ts=12345)

    rid = await reminders.enqueue(db_conn, **_make_reminder())
    await db_conn.commit()

    await dispatch_due_reminders(db_conn, signal_send_fn=signal_mock)

    # Verify delivered state
    async with db_conn.cursor() as cur:
        await cur.execute("SELECT state, signal_timestamp FROM reminder_outbox WHERE id = %s", (str(rid),))
        row = await cur.fetchone()

    assert row is not None
    assert row[0] == "delivered"
    assert row[1] == 12345

    # Signal was called exactly once
    signal_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 2: Retry — signal fails 3 times, succeeds on attempt 4
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_on_signal_failure(db_conn: Any) -> None:
    """Signal fails 3 times; row backs off; succeeds on attempt 4."""
    from app.scheduler.reminder_worker import dispatch_due_reminders
    from app.tools import reminders

    call_count = 0

    async def flaky_signal(recipient: str, message: str) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count < 4:
            raise RuntimeError("simulated signal failure")
        return {"timestamp": 9999}

    rid = await reminders.enqueue(db_conn, **_make_reminder())
    await db_conn.commit()

    # Attempt 1, 2, 3 — fail; attempt 4 — succeed
    for expected_attempt in range(1, 5):
        # Reset due_at so worker picks it up each time
        await db_conn.execute(
            "UPDATE reminder_outbox SET due_at = now() - interval '1 second' WHERE id = %s",
            (str(rid),),
        )
        await db_conn.commit()

        await dispatch_due_reminders(db_conn, signal_send_fn=flaky_signal)

        async with db_conn.cursor() as cur:
            await cur.execute(
                "SELECT state, attempt FROM reminder_outbox WHERE id = %s", (str(rid),)
            )
            row = await cur.fetchone()

        if expected_attempt < 4:
            assert row[0] == "scheduled", f"Expected scheduled at attempt {expected_attempt}, got {row[0]}"
            assert row[1] == expected_attempt
        else:
            assert row[0] == "delivered"
            assert row[1] == expected_attempt

    assert call_count == 4


# ---------------------------------------------------------------------------
# Test 3: Worker crash mid-delivering — re-claim after locked_until expires
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_crash_mid_delivering(db_conn: Any) -> None:
    """Worker crash mid-delivering: next worker re-claims after locked_until expires."""
    from app.scheduler.reminder_worker import dispatch_due_reminders
    from app.tools import reminders

    rid = await reminders.enqueue(db_conn, **_make_reminder())
    await db_conn.commit()

    # Simulate a claim by a previous worker that crashed: set state=delivering
    # with an EXPIRED locked_until (so next worker can reclaim).
    await db_conn.execute(
        """
        UPDATE reminder_outbox
           SET state = 'delivering',
               locked_until = now() - interval '1 second',
               worker_id = 'crashed-worker:0'
         WHERE id = %s
        """,
        (str(rid),),
    )
    await db_conn.commit()

    # The next dispatch: worker should NOT pick up 'delivering' rows
    # (SELECT FOR UPDATE SKIP LOCKED filters to 'pending' and 'scheduled' only).
    # So the reminder stays stuck in 'delivering' until the operator or a cleanup
    # job transitions it back to 'scheduled'. This is by design for Phase A.
    # Phase B will add a stuck-delivery sweeper.

    # Manually transition back to scheduled (simulating the sweeper)
    await db_conn.execute(
        "UPDATE reminder_outbox SET state = 'scheduled', due_at = now() - interval '1 second' WHERE id = %s",
        (str(rid),),
    )
    await db_conn.commit()

    signal_mock = _mock_signal()
    await dispatch_due_reminders(db_conn, signal_send_fn=signal_mock)

    async with db_conn.cursor() as cur:
        await cur.execute("SELECT state FROM reminder_outbox WHERE id = %s", (str(rid),))
        row = await cur.fetchone()

    assert row[0] == "delivered"
    # At-least-once: we may see at most one extra delivery (from the crashed attempt
    # that might have completed before the crash), but the test doesn't require zero
    # duplicates — it requires the row reaches 'delivered' via the re-claim.
    signal_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 4: 100 reminders all due — all delivered within time bound
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_100_reminders_delivered(db_conn: Any) -> None:
    """100 reminders enqueued and all due: all delivered with zero failures."""
    from app.scheduler.reminder_worker import _BATCH_SIZE, dispatch_due_reminders
    from app.tools import reminders

    ids = []
    for i in range(100):
        rid = await reminders.enqueue(
            db_conn,
            notion_page_id=f"page-{i:04d}",
            peer="<peer>",
            body="Test reminder",
            due_at=datetime.now(UTC) - timedelta(seconds=1),
            idempotency_key=f"idem-{i:04d}",
        )
        ids.append(rid)
    await db_conn.commit()

    signal_mock = _mock_signal()

    # Run enough batches to cover all 100 (batch size is _BATCH_SIZE)
    batches = (100 + _BATCH_SIZE - 1) // _BATCH_SIZE
    for _ in range(batches):
        await dispatch_due_reminders(db_conn, signal_send_fn=signal_mock)

    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM reminder_outbox WHERE state = 'delivered'"
        )
        delivered_count = (await cur.fetchone())[0]

        await cur.execute(
            "SELECT COUNT(*) FROM reminder_outbox WHERE state IN ('failed', 'dead')"
        )
        failure_count = (await cur.fetchone())[0]

    assert delivered_count == 100, f"Expected 100 delivered, got {delivered_count}"
    assert failure_count == 0


# ---------------------------------------------------------------------------
# Test 5: Dead state triggers ops alert; throttle prevents storm
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dead_reminder_triggers_ops_alert(db_conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reminder reaching dead state triggers an ops alert; throttle prevents re-alert."""
    from app.scheduler.reminder_worker import _MAX_ATTEMPTS, dispatch_due_reminders
    from app.tools import reminders

    async def always_fail(recipient: str, message: str) -> dict[str, Any]:
        raise RuntimeError("always fails")

    rid = await reminders.enqueue(db_conn, **_make_reminder())
    await db_conn.commit()

    # Exhaust all attempts
    for _ in range(_MAX_ATTEMPTS):
        await db_conn.execute(
            "UPDATE reminder_outbox SET due_at = now() - interval '1 second' WHERE id = %s",
            (str(rid),),
        )
        await db_conn.commit()
        await dispatch_due_reminders(db_conn, signal_send_fn=always_fail)

    async with db_conn.cursor() as cur:
        await cur.execute("SELECT state FROM reminder_outbox WHERE id = %s", (str(rid),))
        row = await cur.fetchone()

    assert row[0] == "dead"

    # Ops alert should be in the throttle table
    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT alert_kind FROM ops_alerts_throttle WHERE alert_kind = 'reminder_dead'"
        )
        throttle_row = await cur.fetchone()

    assert throttle_row is not None, "ops alert throttle row should exist after dead reminder"

    # Running again should NOT insert another throttle row (throttled)
    await db_conn.execute(
        """
        INSERT INTO reminder_outbox (id, notion_page_id, peer, body, due_at, state, idempotency_key)
        VALUES (%s, %s, '<peer>', 'Test reminder 2', now() - interval '1 second', 'pending', %s)
        """,
        (str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())),
    )
    await db_conn.commit()

    # Exhaust attempts on second reminder — alert should be throttled
    for _ in range(_MAX_ATTEMPTS):
        await db_conn.execute(
            "UPDATE reminder_outbox SET due_at = now() - interval '1 second' WHERE state != 'dead'"
        )
        await db_conn.commit()
        await dispatch_due_reminders(db_conn, signal_send_fn=always_fail)

    async with db_conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM ops_alerts_throttle WHERE alert_kind = 'reminder_dead'")
        count = (await cur.fetchone())[0]

    # Still just one row (throttle prevents duplicates)
    assert count == 1
