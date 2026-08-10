"""User time context helper.

Port of scripts/user-time-context.sh — resolves a reference timestamp
(or "now") to the user's local calendar context for reminder intake.

Reads the user's IANA timezone from the USER_TZ environment variable first.
Falls back to USER.md when USER_TZ is not set, then to DEFAULT_TZ or "America/Chicago".
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_REPO_ROOT = Path(__file__).parent.parent.parent
_USER_FILE = _REPO_ROOT / "USER.md"
_DEFAULT_TZ = os.environ.get("DEFAULT_TZ", "America/Chicago")


class TimeContext(TypedDict):
    user_timezone: str
    reference_utc: str
    reference_local: str
    local_date: str
    local_day_of_week: str
    tomorrow_date: str
    tomorrow_day_of_week: str


def _extract_timezone_from_user_md() -> str | None:
    """Parse ``- **Timezone:** ... (IANA/Identifier)`` from USER.md."""
    if not _USER_FILE.is_file():
        return None
    import re

    pattern = re.compile(r"^- \*\*Timezone:\*\* .* \(([^()]+)\)$", re.MULTILINE)
    match = pattern.search(_USER_FILE.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def _resolve_tz(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        if tz_name != _DEFAULT_TZ:
            return _resolve_tz(_DEFAULT_TZ)
        raise


def get_time_context(reference_input: str = "now") -> TimeContext:
    """Return calendar context dict, mirroring scripts/user-time-context.sh output.

    Args:
        reference_input: ISO 8601 timestamp string, or "now" for current time.

    Returns:
        TimeContext dict with keys: user_timezone, reference_utc, reference_local,
        local_date, local_day_of_week, tomorrow_date, tomorrow_day_of_week.
    """
    # Resolve timezone: USER_TZ env var takes priority over USER.md legacy file.
    user_tz_name = os.environ.get("USER_TZ") or _extract_timezone_from_user_md() or _DEFAULT_TZ
    user_tz = _resolve_tz(user_tz_name)

    # Parse reference timestamp
    if reference_input.strip() == "now":
        reference_utc = datetime.now(UTC)
    else:
        normalized = reference_input.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        reference_utc = parsed.astimezone(UTC)

    reference_local = reference_utc.astimezone(user_tz)
    tomorrow_local_date = reference_local.date() + timedelta(days=1)
    tomorrow_local = datetime.combine(
        tomorrow_local_date,
        reference_local.timetz().replace(tzinfo=None),
        tzinfo=user_tz,
    )

    return TimeContext(
        user_timezone=user_tz_name,
        reference_utc=reference_utc.isoformat().replace("+00:00", "Z"),
        reference_local=reference_local.isoformat(),
        local_date=reference_local.date().isoformat(),
        local_day_of_week=reference_local.strftime("%A"),
        tomorrow_date=tomorrow_local_date.isoformat(),
        tomorrow_day_of_week=tomorrow_local.strftime("%A"),
    )
