"""Unit tests for app.tools.time_context — port of user-time-context.sh."""
from __future__ import annotations

from app.tools.time_context import get_time_context


def test_now_returns_expected_keys() -> None:
    """get_time_context('now') returns all required keys."""
    ctx = get_time_context("now")
    required_keys = {
        "user_timezone",
        "reference_utc",
        "reference_local",
        "local_date",
        "local_day_of_week",
        "tomorrow_date",
        "tomorrow_day_of_week",
    }
    assert required_keys <= set(ctx.keys())


def test_explicit_timestamp_parsed_correctly() -> None:
    """Explicit UTC timestamp is parsed and localised correctly."""
    # 2026-01-01T00:00:00Z is Dec 31 in America/Chicago (UTC-6)
    ctx = get_time_context("2026-01-01T00:00:00Z")
    assert ctx["reference_utc"] == "2026-01-01T00:00:00Z"
    assert ctx["local_date"] == "2025-12-31"
    assert ctx["local_day_of_week"] == "Wednesday"
    assert ctx["tomorrow_date"] == "2026-01-01"


def test_z_suffix_normalised() -> None:
    """Timestamps ending in Z are parsed without error."""
    ctx = get_time_context("2026-06-15T12:00:00Z")
    assert ctx["reference_utc"].endswith("Z")


def test_explicit_offset_timestamp() -> None:
    """ISO timestamp with explicit offset is handled."""
    # 2026-03-01T18:00:00-06:00 is 2026-03-02T00:00:00Z
    ctx = get_time_context("2026-03-01T18:00:00-06:00")
    assert ctx["reference_utc"] == "2026-03-02T00:00:00Z"


def test_tomorrow_is_next_day() -> None:
    """tomorrow_date is exactly one day after local_date."""
    from datetime import date
    ctx = get_time_context("2026-07-04T10:00:00Z")
    local = date.fromisoformat(ctx["local_date"])
    tomorrow = date.fromisoformat(ctx["tomorrow_date"])
    assert (tomorrow - local).days == 1
