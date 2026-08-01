"""Scheduling domain service helpers — pure date/time/ownership logic (no DB, no I/O).

Vendor- and locale-neutral. Times are validated/normalized to "HH:MM" and dates to
ISO "YYYY-MM-DD" so the storage layer stays on the asyncpg-safe TEXT path. Shift
durations are computed here (overnight-safe) rather than trusting a DB interval.
"""

from __future__ import annotations

from datetime import date, timedelta

from be.app.domains.hr.service import normalize_time  # reuse the "HH:MM" validator

VALID_SHIFT_STATUSES: tuple[str, ...] = (
    "draft",
    "pending",
    "approved",
    "declined",
    "cancelled",
)

VALID_TIME_OFF_TYPES: tuple[str, ...] = ("vacation", "sick")


def parse_date(raw: str | None) -> date:
    """Parse an ISO "YYYY-MM-DD" string; raise :class:`ValueError` if malformed."""
    if not raw:
        raise ValueError("date is required")
    return date.fromisoformat(raw.strip())


def require_time(raw: str | None) -> str:
    """Normalize a required "HH:MM" time; raise :class:`ValueError` if empty/bad."""
    norm = normalize_time(raw)
    if norm is None:
        raise ValueError("time is required")
    return norm


def sunday_of_week(d: date) -> date:
    """Return the Sunday on/before *d* (weeks run Sun..Sat, matching the FE)."""
    # isoweekday(): Mon=1 .. Sun=7 -> %7 gives Sun=0 .. Sat=6.
    return d - timedelta(days=d.isoweekday() % 7)


def is_sunday(d: date) -> bool:
    return d.isoweekday() % 7 == 0


def day_of_week(d: date) -> int:
    """0=Sun .. 6=Sat for *d* (matches recurring_shifts.day_of_week)."""
    return d.isoweekday() % 7


def shift_minutes(start: str | None, end: str | None) -> int | None:
    """Worked minutes from "HH:MM" wall-clock start/end, +24h when crossing midnight.

    Mirrors the live payroll math so the staff view agrees with admin; ``None`` if
    either bound is missing (an open shift).
    """
    if not start or not end:
        return None
    sh, sm = (int(x) for x in start.split(":"))
    eh, em = (int(x) for x in end.split(":"))
    minutes = (eh * 60 + em) - (sh * 60 + sm)
    if minutes < 0:
        minutes += 24 * 60
    return minutes


def minutes_to_duration(minutes: int | None) -> str | None:
    """Interval-style "H:MM:00" string the staff FE parses; ``None`` passthrough."""
    if minutes is None:
        return None
    return f"{minutes // 60}:{minutes % 60:02d}:00"


def month_bounds(year: int, month: int) -> tuple[str, str]:
    """Return (first_day, last_day) of *year*/*month* as ISO strings."""
    first = date(year, month, 1)
    last = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return first.isoformat(), (last - timedelta(days=1)).isoformat()


def month_is_closed(year: int, month: int, today: date) -> bool:
    """A month is closed for staff corrections once *today* is in a later month."""
    return (year, month) < (today.year, today.month)


__all__ = [
    "VALID_SHIFT_STATUSES",
    "VALID_TIME_OFF_TYPES",
    "parse_date",
    "require_time",
    "sunday_of_week",
    "is_sunday",
    "day_of_week",
    "shift_minutes",
    "minutes_to_duration",
    "month_bounds",
    "month_is_closed",
]
