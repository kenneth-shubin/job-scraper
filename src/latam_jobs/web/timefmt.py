"""Local display of UTC timestamps."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from latam_jobs.scheduler import from_iso

_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def format_posted(value: datetime | str | None, timezone: str, now: datetime) -> str:
    parsed = from_iso(value) if isinstance(value, str) else value
    if parsed is None:
        return "—"
    return _relative(parsed, now)


def format_when(value: datetime | None, timezone: str, now: datetime) -> str:
    if value is None:
        return "Never"
    local = value.astimezone(ZoneInfo(timezone))
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    clock = (
        f"{_MONTHS[local.month - 1]} {local.day}, {local.year}, {hour}:{local.minute:02d} {suffix}"
    )
    return f"{clock} ({_relative(value, now)})"


def _relative(value: datetime, now: datetime) -> str:
    seconds = int((now - value).total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit} ago"
    hours = minutes // 60
    if hours < 24:
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit} ago"
    days = hours // 24
    unit = "day" if days == 1 else "days"
    return f"{days} {unit} ago"
