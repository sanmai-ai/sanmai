"""Tasks/checklists domain service helpers — pure logic (no DB, no I/O).

Vendor- and locale-neutral. Times are validated/normalized to "HH:MM" and dates to
ISO "YYYY-MM-DD" so the storage layer stays on the asyncpg-safe TEXT path. JSON
columns are parsed/dumped here so the crud never trusts a raw driver value.

Window-overlap and recurrence math live here (not the DB) so the same rules hold on
sqlite (tests) and postgres (prod).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from be.app.domains.hr.service import normalize_time  # reuse the "HH:MM" validator

VALID_TASK_CATEGORIES: tuple[str, ...] = ("opening", "closing", "during_shift")
VALID_PROOF_TYPES: tuple[str, ...] = ("check", "text", "photo")
VALID_SCHEDULE_SCOPES: tuple[str, ...] = ("anyone", "scheduled")

MAX_SUBTASKS = 20  # a helper checklist inside one task; keep it small
MAX_PROOF_PHOTOS = 5
MAX_TEMPLATE_PHOTOS = 5


# --- date / time ------------------------------------------------------------


def parse_date(raw: str | None) -> date:
    """Parse an ISO "YYYY-MM-DD" string; raise :class:`ValueError` if malformed."""
    if not raw:
        raise ValueError("date is required")
    return date.fromisoformat(raw.strip())


def parse_hhmm(raw: str | None) -> str | None:
    """Normalize an optional "HH:MM" time; ``None``/empty -> ``None``.

    Raises :class:`ValueError` on a malformed/out-of-range time.
    """
    return normalize_time(raw)


def day_of_week(d: date) -> int:
    """0=Sun .. 6=Sat for *d* (matches recurrence weekday ints)."""
    return d.isoweekday() % 7


# --- validation -------------------------------------------------------------


def validate_category(category: str | None) -> None:
    if category is not None and category not in VALID_TASK_CATEGORIES:
        raise ValueError(
            f"invalid category (allowed: {', '.join(VALID_TASK_CATEGORIES)})"
        )


def validate_proof_type(proof_type: str | None) -> None:
    if proof_type is not None and proof_type not in VALID_PROOF_TYPES:
        raise ValueError(
            f"invalid proof type (allowed: {', '.join(VALID_PROOF_TYPES)})"
        )


def validate_required_staff(required_staff: int | None) -> None:
    if required_staff is not None and required_staff < 1:
        raise ValueError("required_staff must be >= 1")


def validate_recurrence(recurrence: list[int] | None) -> None:
    if recurrence is not None and any((d < 0 or d > 6) for d in recurrence):
        raise ValueError("recurrence weekdays must be 0 (Sun) .. 6 (Sat)")


def clean_subtasks(subtasks: list[Any] | None) -> list[str]:
    """Trim, drop blanks, cap at :data:`MAX_SUBTASKS`. Order is preserved."""
    out: list[str] = []
    for s in subtasks or []:
        if not isinstance(s, str):
            continue
        t = s.strip()
        if t:
            out.append(t)
    if len(out) > MAX_SUBTASKS:
        raise ValueError(f"too many subtasks (max {MAX_SUBTASKS})")
    return out


# --- json helpers -----------------------------------------------------------


def json_load(value: Any, default: Any) -> Any:
    """Parse a JSON-as-TEXT column; tolerate a driver that already parsed it."""
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return default
    return value


def json_dump(value: Any) -> str:
    """Serialize a Python value to a JSON-as-TEXT column payload."""
    return json.dumps(value, default=str)


# --- overlap / recurrence ---------------------------------------------------


def windows_overlap(
    shift_start: str | None,
    shift_end: str | None,
    win_start: str | None,
    win_end: str | None,
) -> bool:
    """True when a shift's "HH:MM" window overlaps the assignee window.

    A missing window (either bound ``None``) means "no constraint" -> overlap.
    String compare is valid for zero-padded "HH:MM".
    """
    if win_start is None or win_end is None:
        return True
    if shift_start is None or shift_end is None:
        return True
    return shift_start < win_end and shift_end > win_start


def recurrence_includes(recurrence: Any, d: date) -> bool:
    """True if *d*'s weekday is in the checklist's recurrence (NULL/[] = every day)."""
    days = json_load(recurrence, None)
    if not days:  # NULL / [] = every day
        return True
    return day_of_week(d) in days


def split_bonus(amount: Decimal, n: int) -> list[Decimal]:
    """Equal split into *n* round-2dp shares, remainder to the earliest finisher.

    DEFERRED helper kept for the payroll domain (bonus crediting is not wired in
    this increment — see the hook in :func:`crud.finish_task`). Rows always sum to
    exactly *amount*.
    """
    if n <= 0:
        return []
    share = (amount / n).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    remainder = amount - share * n
    return [share + remainder if i == 0 else share for i in range(n)]


__all__ = [
    "VALID_TASK_CATEGORIES",
    "VALID_PROOF_TYPES",
    "VALID_SCHEDULE_SCOPES",
    "MAX_SUBTASKS",
    "MAX_PROOF_PHOTOS",
    "MAX_TEMPLATE_PHOTOS",
    "parse_date",
    "parse_hhmm",
    "day_of_week",
    "validate_category",
    "validate_proof_type",
    "validate_required_staff",
    "validate_recurrence",
    "clean_subtasks",
    "json_load",
    "json_dump",
    "windows_overlap",
    "recurrence_includes",
    "split_bonus",
]
