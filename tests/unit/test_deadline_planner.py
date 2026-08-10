"""Unit tests for deadline milestone planning."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.scheduler.deadline_planner import (
    PlannerConfig,
    assign_slot,
    format_reminder_summary,
    plan_milestones,
    select_tier,
)

_TZ = "America/Chicago"


def _dt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_tier_selection() -> None:
    assert select_tier(85) == "dense"
    assert select_tier(50) == "standard"
    assert select_tier(30) == "sparse"


def test_dense_milestone_table() -> None:
    now = _dt(2026, 1, 1, 16)
    deadline = _dt(2026, 5, 1, 22)

    milestones = plan_milestones(deadline, urgency=90, now=now, user_tz=_TZ)

    assert [m.label for m in milestones] == ["90d", "30d", "14d", "7d", "3d", "1d", "4h"]


def test_standard_clips_past_milestones() -> None:
    now = _dt(2026, 6, 1, 16)
    deadline = _dt(2026, 6, 6, 22)

    milestones = plan_milestones(deadline, urgency=50, now=now, user_tz=_TZ)

    assert [m.label for m in milestones] == ["3d", "1d", "4h"]


def test_sparse_keeps_final_four_hour_ping() -> None:
    now = _dt(2026, 6, 1, 16)
    deadline = _dt(2026, 6, 2, 22)

    milestones = plan_milestones(deadline, urgency=20, now=now, user_tz=_TZ)

    assert [m.label for m in milestones] == ["4h"]


def test_quiet_hours_pushes_to_end_of_quiet_window() -> None:
    ideal = _dt(2026, 6, 3, 11)  # 6am America/Chicago
    deadline = _dt(2026, 6, 6, 22)
    now = _dt(2026, 6, 1, 13)

    assigned, fallback = assign_slot(
        ideal,
        [],
        deadline,
        now=now,
        user_tz=_TZ,
        config=PlannerConfig(quiet_start_hour=22, quiet_end_hour=8),
    )

    assert not fallback
    assert assigned.hour == 13  # 8am America/Chicago in June


def test_full_bucket_walks_to_open_slot_within_drift() -> None:
    ideal = _dt(2026, 6, 3, 15)
    deadline = _dt(2026, 6, 6, 22)
    now = _dt(2026, 6, 1, 13)
    existing = [ideal, ideal + timedelta(minutes=5)]

    assigned, fallback = assign_slot(
        ideal,
        existing,
        deadline,
        now=now,
        user_tz=_TZ,
        config=PlannerConfig(slot_capacity=2, slot_minutes=30),
    )

    assert not fallback
    assert assigned != ideal
    assert abs((assigned - ideal).total_seconds()) <= 30 * 60


def test_fallback_when_every_bucket_is_full() -> None:
    ideal = _dt(2026, 6, 3, 15)
    deadline = _dt(2026, 6, 6, 22)
    now = _dt(2026, 6, 1, 13)
    existing = []
    for offset in (-30, 0, 30):
        existing.extend([ideal + timedelta(minutes=offset)] * 2)

    assigned, fallback = assign_slot(
        ideal,
        existing,
        deadline,
        now=now,
        user_tz=_TZ,
        config=PlannerConfig(slot_capacity=2, slot_minutes=30, max_drift_minutes=30),
    )

    assert fallback
    assert assigned == ideal


def test_format_reminder_summary_is_chronological() -> None:
    slots = [
        ("1d", _dt(2026, 6, 4, 14)),
        ("3d", _dt(2026, 6, 3, 17)),
        ("4h", _dt(2026, 6, 4, 13)),
    ]

    assert format_reminder_summary(slots, _TZ) == "I'll ping you Wed noon, Thu 8am, and Thu 9am."
