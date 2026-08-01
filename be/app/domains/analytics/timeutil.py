"""Time helpers for the analytics domain — ISO strings + portable heatmap keys.

All timestamps/dates are written as ISO-8601 TEXT (the migration stores no native
date/time types), and the day-of-week / hour heatmap keys are computed here once at
ingest so every read query stays dialect-portable (no ``EXTRACT``/``AT TIME ZONE``).
The venue timezone is applied at ingest when a naive timestamp is parsed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

DEFAULT_TZ = ZoneInfo("Asia/Jerusalem")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_ts(value: str | None, *, tz: ZoneInfo = DEFAULT_TZ) -> datetime | None:
    """Parse an ISO-ish datetime string; localize naive values to ``tz``."""
    if not value:
        return None
    s = str(value).strip().replace(" UTC", "")
    # Accept a trailing 'Z' and a space between date and time.
    s = s.replace("Z", "+00:00")
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    for candidate in (s, s.split(".", 1)[0]):
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz)
    return None


def dow_sun0(dt: datetime) -> int:
    """Day of week as 0=Sun..6=Sat (Python ``weekday()`` is Mon=0..Sun=6)."""
    return (dt.weekday() + 1) % 7


__all__ = ["DEFAULT_TZ", "now_iso", "parse_ts", "dow_sun0"]
