"""Pure deadline milestone planning utilities."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

Tier = Literal["dense", "standard", "sparse"]

_TIER_OFFSETS: dict[Tier, list[tuple[str, timedelta]]] = {
    "dense": [
        ("90d", timedelta(days=90)),
        ("30d", timedelta(days=30)),
        ("14d", timedelta(days=14)),
        ("7d", timedelta(days=7)),
        ("3d", timedelta(days=3)),
        ("1d", timedelta(days=1)),
        ("4h", timedelta(hours=4)),
    ],
    "standard": [
        ("60d", timedelta(days=60)),
        ("14d", timedelta(days=14)),
        ("7d", timedelta(days=7)),
        ("3d", timedelta(days=3)),
        ("1d", timedelta(days=1)),
        ("4h", timedelta(hours=4)),
    ],
    "sparse": [
        ("60d", timedelta(days=60)),
        ("14d", timedelta(days=14)),
        ("3d", timedelta(days=3)),
        ("4h", timedelta(hours=4)),
    ],
}

_MIN_FUTURE_GUARD = timedelta(minutes=15)


@dataclass(frozen=True)
class PlannerConfig:
    slot_minutes: int = 30
    slot_capacity: int = 2
    quiet_start_hour: int = 22
    quiet_end_hour: int = 8
    max_drift_minutes: int = 720


@dataclass(frozen=True)
class Milestone:
    label: str
    tier: Tier
    ideal_at: datetime


def config_from_env() -> PlannerConfig:
    """Read planner knobs from environment with conservative defaults."""
    return PlannerConfig(
        slot_minutes=_env_int("REMINDER_SLOT_MINUTES", 30, minimum=1),
        slot_capacity=_env_int("REMINDER_SLOT_CAPACITY", 2, minimum=1),
        quiet_start_hour=_env_int("REMINDER_QUIET_START_HOUR", 22, minimum=0, maximum=23),
        quiet_end_hour=_env_int("REMINDER_QUIET_END_HOUR", 8, minimum=0, maximum=23),
    )


def select_tier(urgency: int) -> Tier:
    """Map intake urgency to a milestone density tier."""
    if urgency >= 80:
        return "dense"
    if urgency <= 30:
        return "sparse"
    return "standard"


def plan_milestones(
    deadline_at: datetime,
    *,
    urgency: int,
    now: datetime,
    user_tz: str,
) -> list[Milestone]:
    """Return future milestone ideals for a deadline, sorted by time."""
    deadline = _aware_utc(deadline_at).astimezone(ZoneInfo(user_tz))
    current = _aware_utc(now).astimezone(ZoneInfo(user_tz))
    tier = select_tier(urgency)

    if deadline <= current + _MIN_FUTURE_GUARD:
        return []

    milestones = [
        Milestone(label=label, tier=tier, ideal_at=(deadline - offset).astimezone(UTC))
        for label, offset in _TIER_OFFSETS[tier]
        if deadline - offset > current + _MIN_FUTURE_GUARD
    ]
    return sorted(milestones, key=lambda m: m.ideal_at)


def assign_slot(
    ideal_at: datetime,
    existing_slots: list[datetime],
    deadline_at: datetime,
    *,
    now: datetime,
    user_tz: str,
    config: PlannerConfig | None = None,
) -> tuple[datetime, bool]:
    """Choose a quiet-hours-aware, load-balanced slot near an ideal time.

    Returns ``(assigned_at, used_fallback)``. Fallback means no open bucket was
    found inside the drift window, so the caller should log a warning but still
    preserve the milestone.
    """
    cfg = config or PlannerConfig()
    ideal = _snap_forward(_aware_utc(ideal_at), cfg.slot_minutes)
    deadline = _aware_utc(deadline_at)
    current = _aware_utc(now)
    buckets = _bucket_counts(existing_slots, cfg.slot_minutes)
    step = timedelta(minutes=cfg.slot_minutes)
    max_steps = max(0, cfg.max_drift_minutes // cfg.slot_minutes)

    for index in _search_offsets(max_steps):
        candidate = ideal + (step * index)
        candidate = _move_out_of_quiet_hours(
            candidate,
            user_tz=user_tz,
            quiet_start=cfg.quiet_start_hour,
            quiet_end=cfg.quiet_end_hour,
            slot_minutes=cfg.slot_minutes,
        )
        if candidate < current + _MIN_FUTURE_GUARD:
            continue
        if candidate >= deadline:
            continue
        if _is_quiet(candidate, user_tz, cfg.quiet_start_hour, cfg.quiet_end_hour):
            continue
        bucket = _bucket_start(candidate, cfg.slot_minutes)
        if buckets.get(bucket, 0) < cfg.slot_capacity:
            return candidate, False

    fallback = _move_out_of_quiet_hours(
        ideal,
        user_tz=user_tz,
        quiet_start=cfg.quiet_start_hour,
        quiet_end=cfg.quiet_end_hour,
        slot_minutes=cfg.slot_minutes,
    )
    if fallback >= deadline:
        fallback = ideal
    return fallback, True


def format_reminder_summary(slots: list[tuple[str, datetime]], user_tz: str) -> str:
    """Format assigned slots into a short user-facing confirmation suffix."""
    if not slots:
        return ""
    zone = ZoneInfo(user_tz)
    labels = [_format_local_time(dt.astimezone(zone)) for _, dt in sorted(slots, key=lambda s: s[1])]
    if len(labels) == 1:
        joined = labels[0]
    elif len(labels) == 2:
        joined = f"{labels[0]} and {labels[1]}"
    else:
        joined = f"{', '.join(labels[:-1])}, and {labels[-1]}"
    return f"I'll ping you {joined}."


def _env_int(name: str, default: int, *, minimum: int, maximum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    if value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _bucket_start(value: datetime, slot_minutes: int) -> datetime:
    utc = _aware_utc(value)
    minute = (utc.minute // slot_minutes) * slot_minutes
    return utc.replace(minute=minute, second=0, microsecond=0)


def _snap_forward(value: datetime, slot_minutes: int) -> datetime:
    bucket = _bucket_start(value, slot_minutes)
    if bucket == _aware_utc(value).replace(second=0, microsecond=0):
        return bucket
    return bucket + timedelta(minutes=slot_minutes)


def _bucket_counts(existing_slots: list[datetime], slot_minutes: int) -> dict[datetime, int]:
    counts: dict[datetime, int] = {}
    for slot in existing_slots:
        bucket = _bucket_start(slot, slot_minutes)
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def _search_offsets(max_steps: int) -> list[int]:
    offsets = [0]
    for step in range(1, max_steps + 1):
        offsets.extend([-step, step])
    return offsets


def _is_quiet(value: datetime, user_tz: str, quiet_start: int, quiet_end: int) -> bool:
    hour = value.astimezone(ZoneInfo(user_tz)).hour
    if quiet_start == quiet_end:
        return False
    if quiet_start < quiet_end:
        return quiet_start <= hour < quiet_end
    return hour >= quiet_start or hour < quiet_end


def _move_out_of_quiet_hours(
    value: datetime,
    *,
    user_tz: str,
    quiet_start: int,
    quiet_end: int,
    slot_minutes: int,
) -> datetime:
    if not _is_quiet(value, user_tz, quiet_start, quiet_end):
        return value
    zone = ZoneInfo(user_tz)
    local = value.astimezone(zone)
    target_day = local.date()
    if quiet_start > quiet_end and local.hour >= quiet_start:
        target_day += timedelta(days=1)
    target = datetime.combine(target_day, datetime.min.time(), tzinfo=zone).replace(
        hour=quiet_end,
        minute=0,
    )
    return _snap_forward(target.astimezone(UTC), slot_minutes)


def _format_local_time(value: datetime) -> str:
    day = value.strftime("%a")
    if value.hour == 0 and value.minute == 0:
        time_label = "midnight"
    elif value.hour == 12 and value.minute == 0:
        time_label = "noon"
    else:
        hour = value.hour % 12 or 12
        suffix = "am" if value.hour < 12 else "pm"
        if value.minute:
            time_label = f"{hour}:{value.minute:02d}{suffix}"
        else:
            time_label = f"{hour}{suffix}"
    return f"{day} {time_label}"
