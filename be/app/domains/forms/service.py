"""Forms/courses/flows domain service helpers — pure logic (no DB, no I/O).

Vendor- and locale-neutral. JSON columns are parsed/dumped here so the crud never
trusts a raw driver value; timestamps are ISO-8601 TEXT (asyncpg-safe) and all date
math (per-item ``due_at``, overdue, the derived assignment status bucket) is done in
Python so the same rules hold on sqlite (tests) and postgres (prod).

The course-completion set math (which the live code did in-DB with postgres ``TEXT[]``
+ ``UNNEST``) and the flow assignment-progress materialisation (parallel/sequential
unlock) live here too.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

# --- enums ------------------------------------------------------------------

VALID_BINDING_LANGUAGES: tuple[str, ...] = ("he", "en", "none")
VALID_TEMPLATE_STATUS: tuple[str, ...] = ("draft", "published", "archived")
VALID_COURSE_ITEM_TYPES: tuple[str, ...] = ("text", "image", "video", "quiz")
VALID_FLOW_ORDERING: tuple[str, ...] = ("parallel", "sequential")
VALID_FLOW_ITEM_TYPES: tuple[str, ...] = ("form", "course")
VALID_ASSIGNMENT_SOURCES: tuple[str, ...] = ("flow", "standalone")
VALID_PROGRESS_STATUS: tuple[str, ...] = ("locked", "available", "in_progress", "completed")

# Derived (read-time) assignment buckets + admin list sort keys.
VALID_ASSIGNMENT_STATUS: frozenset[str] = frozenset(
    {"not_started", "in_progress", "completed", "overdue"}
)
VALID_ASSIGNMENT_SORT: frozenset[str] = frozenset(
    {"due_at_asc", "due_at_desc", "assigned_at_desc", "employee"}
)


# --- ids / time -------------------------------------------------------------


def new_id() -> str:
    """Allocate a fresh app-side surrogate id (UUID string)."""
    return str(uuid.uuid4())


def now_iso() -> str:
    """Current UTC instant as an ISO-8601 string (the TEXT-timestamp convention)."""
    return datetime.now(UTC).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 TEXT timestamp to an aware datetime, or ``None``."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def compute_due_at(
    assigned_at_iso: str,
    default_due_days: int | None,
    override_days: int | None,
) -> str | None:
    """Per-item ``due_at`` = assigned_at + (override OR default) days, as ISO TEXT.

    Snapshot semantics: resolved once at assignment-creation time so later flow edits
    never retroactively reschedule. ``None`` when neither an override nor a default is
    set.
    """
    days = override_days if override_days is not None else default_due_days
    if days is None:
        return None
    base = parse_iso(assigned_at_iso) or datetime.now(UTC)
    return (base + timedelta(days=int(days))).isoformat()


# --- validation -------------------------------------------------------------


def validate_binding_language(value: str | None) -> None:
    if value is not None and value not in VALID_BINDING_LANGUAGES:
        raise ValueError(
            f"invalid binding_language (allowed: {', '.join(VALID_BINDING_LANGUAGES)})"
        )


def validate_course_item_type(value: str | None) -> None:
    if value is not None and value not in VALID_COURSE_ITEM_TYPES:
        raise ValueError(
            f"invalid course item type (allowed: {', '.join(VALID_COURSE_ITEM_TYPES)})"
        )


def validate_flow_ordering(value: str | None) -> None:
    if value is not None and value not in VALID_FLOW_ORDERING:
        raise ValueError(
            f"invalid ordering (allowed: {', '.join(VALID_FLOW_ORDERING)})"
        )


def validate_flow_item_type(value: str | None) -> None:
    if value is not None and value not in VALID_FLOW_ITEM_TYPES:
        raise ValueError(
            f"invalid item_type (allowed: {', '.join(VALID_FLOW_ITEM_TYPES)})"
        )


def validate_assignment_source(value: str | None) -> None:
    if value is not None and value not in VALID_ASSIGNMENT_SOURCES:
        raise ValueError(
            f"invalid source (allowed: {', '.join(VALID_ASSIGNMENT_SOURCES)})"
        )


def normalize_assignment_status(value: str | None) -> str | None:
    """Map an incoming status filter to a known bucket, else ``None`` (all rows)."""
    return value if value in VALID_ASSIGNMENT_STATUS else None


def normalize_assignment_sort(value: str | None) -> str:
    """Map an incoming sort to a known key, else the default ``due_at_asc``."""
    return value if value in VALID_ASSIGNMENT_SORT else "due_at_asc"


def template_has_signature_field(fields: Any) -> bool:
    """True when any field in the (parsed or raw) fields list is a signature field."""
    items = json_load(fields, []) if isinstance(fields, str) else (fields or [])
    if not isinstance(items, list):
        return False
    return any(
        isinstance(f, dict) and str(f.get("type", "")).lower() == "signature"
        for f in items
    )


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


# --- course completion set math ---------------------------------------------


def merge_completed_item(existing: Any, item_id: str) -> list[str]:
    """Append *item_id* to the completed set, de-duped, preserving first-seen order.

    Replaces the live postgres ``ARRAY(SELECT DISTINCT UNNEST(...))`` done in-DB.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in json_load(existing, []) or []:
        sid = str(raw)
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    if item_id not in seen:
        out.append(item_id)
    return out


def course_complete(required_item_ids: list[str], completed_item_ids: list[str]) -> bool:
    """True when every required course item id is present in the completed set."""
    if not required_item_ids:
        return False
    return set(required_item_ids).issubset({str(c) for c in completed_item_ids})


# --- assignment progress rollup (read-time, in Python) ----------------------


def initial_progress_status(ordering: str, index: int) -> str:
    """Unlock rule for a freshly-materialised flow item at position *index*."""
    if ordering == "parallel":
        return "available"
    return "available" if index == 0 else "locked"


def rollup_progress(rows: list[dict], now: datetime | None = None) -> dict:
    """Derive an assignment's aggregate progress from its progress rows.

    Returns ``{total_items, completed_items, next_due_at, status, is_overdue}`` where
    ``status`` is the mutually-exclusive bucket (not_started | in_progress | completed)
    and ``is_overdue`` is an independent overlay (any incomplete row past its due_at).
    """
    now = now or datetime.now(UTC)
    total = len(rows)
    completed = sum(1 for r in rows if r.get("status") == "completed")
    started = sum(1 for r in rows if r.get("status") in ("in_progress", "completed"))

    incomplete_dues: list[datetime] = []
    for r in rows:
        if r.get("status") == "completed":
            continue
        parsed = parse_iso(r.get("due_at"))
        if parsed is not None:
            incomplete_dues.append(parsed)
    next_due_at = min(incomplete_dues).isoformat() if incomplete_dues else None
    is_overdue = any(d < now for d in incomplete_dues)

    if total == 0:
        status = "not_started"
    elif completed == total:
        status = "completed"
    elif started > 0:
        status = "in_progress"
    else:
        status = "not_started"

    return {
        "total_items": total,
        "completed_items": completed,
        "next_due_at": next_due_at,
        "status": status,
        "is_overdue": is_overdue,
    }


def matches_status_filter(rollup: dict, status_filter: str | None) -> bool:
    """True if a rolled-up assignment matches the (already-normalized) status filter."""
    if status_filter is None:
        return True
    if status_filter == "overdue":
        return bool(rollup["is_overdue"])
    return rollup["status"] == status_filter


def sort_assignments(rows: list[dict], sort: str) -> list[dict]:
    """Stable-sort admin/staff assignment rows by the requested key (NULLs last)."""
    far_future = datetime.max.replace(tzinfo=UTC)
    far_past = datetime.min.replace(tzinfo=UTC)

    def _next_due(item: dict) -> datetime | None:
        return parse_iso(item.get("next_due_at") or item.get("due_at"))

    def _key(item: dict) -> tuple:
        if sort == "assigned_at_desc":
            return (-((parse_iso(item.get("assigned_at")) or far_past).timestamp()),)
        if sort == "employee":
            return (item.get("employee_name") or "",)
        nd = _next_due(item)
        if sort == "due_at_desc":
            return (nd is None, -((nd or far_past).timestamp()))
        # default + explicit due_at_asc: soonest first, NULLs last.
        return (nd is None, (nd or far_future).timestamp())

    return sorted(rows, key=_key)


__all__ = [
    "VALID_BINDING_LANGUAGES",
    "VALID_TEMPLATE_STATUS",
    "VALID_COURSE_ITEM_TYPES",
    "VALID_FLOW_ORDERING",
    "VALID_FLOW_ITEM_TYPES",
    "VALID_ASSIGNMENT_SOURCES",
    "VALID_PROGRESS_STATUS",
    "VALID_ASSIGNMENT_STATUS",
    "VALID_ASSIGNMENT_SORT",
    "new_id",
    "now_iso",
    "parse_iso",
    "compute_due_at",
    "validate_binding_language",
    "validate_course_item_type",
    "validate_flow_ordering",
    "validate_flow_item_type",
    "validate_assignment_source",
    "normalize_assignment_status",
    "normalize_assignment_sort",
    "template_has_signature_field",
    "json_load",
    "json_dump",
    "merge_completed_item",
    "course_complete",
    "initial_progress_status",
    "rollup_progress",
    "matches_status_filter",
    "sort_assignments",
]
