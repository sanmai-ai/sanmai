"""Careers domain service helpers — pure logic (no DB, no I/O).

Vendor- and locale-neutral. Department/work-mode/status are validated against fixed
sets; the public-apply body is trimmed + validated here (email regex, phone/experience
minimums) so the checks live server-side, not just in the browser. The CV caps +
filename sanitiser keep uploads bounded and path-safe.

Also hosts a tiny in-process :class:`FixedWindowRateLimiter` for the public endpoints.

DEFERRED (noted): a distributed/production rate-limiter backend — the in-process guard
does NOT share state across Cloud Run instances, so a shared limiter (Redis /
API-gateway) is required for multi-instance prod.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Mapping
from typing import Any

VALID_DEPARTMENTS: tuple[str, ...] = ("kitchen", "service", "bar", "management")
VALID_WORK_MODES: tuple[str, ...] = ("fulltime", "parttime", "shift")
VALID_STATUSES: tuple[str, ...] = ("new", "reviewed", "accepted", "rejected")

MAX_NAME_LEN = 200
MAX_EMAIL_LEN = 254
MAX_PHONE_LEN = 40
MAX_SHORT_TEXT_LEN = 200
MAX_EXPERIENCE_LEN = 5000
MIN_PHONE_DIGITS = 7
MIN_EXPERIENCE_LEN = 20

MAX_CV_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_CV_TYPES: tuple[str, ...] = (
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
)

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PHONE_ALLOWED_RE = re.compile(r"^[\d\s+()\-]+$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


# --------------------------------------------------------------------------- #
# Positions (admin-managed)
# --------------------------------------------------------------------------- #


def normalize_department(raw: str | None) -> str:
    cleaned = (raw or "").strip().lower() or "service"
    if cleaned not in VALID_DEPARTMENTS:
        raise ValueError(f"invalid department (allowed: {', '.join(VALID_DEPARTMENTS)})")
    return cleaned


def normalize_work_mode(raw: str | None) -> str:
    cleaned = (raw or "").strip().lower() or "fulltime"
    if cleaned not in VALID_WORK_MODES:
        raise ValueError(f"invalid work_mode (allowed: {', '.join(VALID_WORK_MODES)})")
    return cleaned


def normalize_string_list(raw: Any) -> list[str]:
    """Coerce an incoming responsibilities/requirements value to a clean string list."""
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [line.strip() for line in raw.splitlines()]
    elif isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    else:
        raise ValueError("expected a list of strings")
    return [x for x in items if x]


def validate_status(raw: str | None) -> str:
    cleaned = (raw or "").strip().lower()
    if cleaned not in VALID_STATUSES:
        raise ValueError(f"invalid status (allowed: {', '.join(VALID_STATUSES)})")
    return cleaned


# --------------------------------------------------------------------------- #
# Applications (public apply — trim + validate server-side)
# --------------------------------------------------------------------------- #


def normalize_email(raw: str | None) -> str:
    cleaned = (raw or "").strip()
    if not cleaned or len(cleaned) > MAX_EMAIL_LEN or not _EMAIL_RE.match(cleaned):
        raise ValueError("invalid email address")
    return cleaned


def _require_short(value: str | None, field: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    if len(cleaned) > MAX_SHORT_TEXT_LEN:
        raise ValueError(f"{field} too long (max {MAX_SHORT_TEXT_LEN} chars)")
    return cleaned


def normalize_application(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Trim + validate a public application body; raise :class:`ValueError` on bad input.

    Mirrors the live client-side validation (email regex, phone/experience minimums)
    server-side so validation is not client-only. Returns normalized column values.
    """
    full_name = (payload.get("full_name") or "").strip()
    if not full_name:
        raise ValueError("full_name is required")
    if len(full_name) > MAX_NAME_LEN:
        raise ValueError(f"full_name too long (max {MAX_NAME_LEN} chars)")

    email = normalize_email(payload.get("email"))

    phone = (payload.get("phone") or "").strip()
    if not phone:
        raise ValueError("phone is required")
    if len(phone) > MAX_PHONE_LEN or not _PHONE_ALLOWED_RE.match(phone):
        raise ValueError("invalid phone number")
    if len(re.sub(r"\D", "", phone)) < MIN_PHONE_DIGITS:
        raise ValueError("invalid phone number")

    city = _require_short(payload.get("city"), "city")
    street = _require_short(payload.get("street"), "street")
    start_date = _require_short(payload.get("start_date"), "start_date")

    experience = (payload.get("experience") or "").strip()
    if len(experience) < MIN_EXPERIENCE_LEN:
        raise ValueError(
            f"experience too short (min {MIN_EXPERIENCE_LEN} chars)"
        )
    if len(experience) > MAX_EXPERIENCE_LEN:
        raise ValueError(f"experience too long (max {MAX_EXPERIENCE_LEN} chars)")

    lang = (payload.get("lang") or "en").strip().lower()
    if lang not in ("en", "he"):
        lang = "en"

    return {
        "full_name": full_name,
        "email": email,
        "phone": phone,
        "city": city,
        "street": street,
        "experience": experience,
        "start_date": start_date,
        "citizenship": bool(payload.get("citizenship")),
        "english": bool(payload.get("english")),
        "lang": lang,
    }


def safe_filename(name: str | None) -> str:
    """Sanitise an upload filename to a path-safe tail (cap length)."""
    cleaned = (name or "cv").strip() or "cv"
    cleaned = _SAFE_NAME_RE.sub("_", cleaned)
    return cleaned[-80:]


# --------------------------------------------------------------------------- #
# Rate limiting (in-process; DEFER a distributed backend)
# --------------------------------------------------------------------------- #


class FixedWindowRateLimiter:
    """A tiny per-key sliding-window guard held in process memory.

    Pluggable seam (the router injects it via a dependency, so a distributed
    implementation can be swapped in later). NOT shared across instances — see the
    module docstring for the DEFERRED distributed-limiter note.
    """

    def __init__(self, *, max_requests: int = 10, window_seconds: float = 60.0) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Record a hit for ``key``; return ``False`` if it exceeds the window budget."""
        ts = time.monotonic() if now is None else now
        bucket = self._hits.setdefault(key, deque())
        cutoff = ts - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.max_requests:
            return False
        bucket.append(ts)
        return True


__all__ = [
    "VALID_DEPARTMENTS",
    "VALID_WORK_MODES",
    "VALID_STATUSES",
    "MAX_NAME_LEN",
    "MAX_EMAIL_LEN",
    "MAX_CV_BYTES",
    "ALLOWED_CV_TYPES",
    "MIN_EXPERIENCE_LEN",
    "normalize_department",
    "normalize_work_mode",
    "normalize_string_list",
    "validate_status",
    "normalize_email",
    "normalize_application",
    "safe_filename",
    "FixedWindowRateLimiter",
]
