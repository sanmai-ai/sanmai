"""Suggestions domain service helpers — pure logic (no DB, no I/O).

Vendor- and locale-neutral. Category/status are validated against fixed sets; the
image caps + filename sanitiser keep uploads bounded and path-safe. Nothing here
touches the database or an adapter — the router/crud call these before persisting.
"""

from __future__ import annotations

import re

VALID_CATEGORIES: tuple[str, ...] = ("foh", "kitchen", "storage", "system", "other")
VALID_STATUSES: tuple[str, ...] = ("open", "closed")

MAX_TITLE_LEN = 200
MAX_EMAIL_LEN = 254
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB per image
ALLOWED_IMAGE_PREFIX = "image/"

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def normalize_category(raw: str | None) -> str:
    """Trim + lowercase a category and validate it; raise :class:`ValueError` if unknown."""
    cleaned = (raw or "").strip().lower() or "other"
    if cleaned not in VALID_CATEGORIES:
        raise ValueError(f"invalid category (allowed: {', '.join(VALID_CATEGORIES)})")
    return cleaned


def normalize_title(raw: str | None) -> str:
    """Trim a title and enforce the length cap. Empty is allowed (draft convention)."""
    cleaned = (raw or "").strip()
    if len(cleaned) > MAX_TITLE_LEN:
        raise ValueError(f"title too long (max {MAX_TITLE_LEN} chars)")
    return cleaned or "Untitled suggestion"


def validate_status(raw: str | None) -> str:
    cleaned = (raw or "").strip().lower()
    if cleaned not in VALID_STATUSES:
        raise ValueError(f"invalid status (allowed: {', '.join(VALID_STATUSES)})")
    return cleaned


def validate_email(raw: str | None) -> str:
    """Minimal, locale-neutral email sanity check for the notification recipient."""
    cleaned = (raw or "").strip()
    if "@" not in cleaned or len(cleaned) > MAX_EMAIL_LEN:
        raise ValueError("invalid email address")
    return cleaned


def safe_filename(name: str | None) -> str:
    """Sanitise an upload filename to a path-safe tail (cap length)."""
    cleaned = (name or "upload").strip() or "upload"
    cleaned = _SAFE_NAME_RE.sub("_", cleaned)
    return cleaned[-80:]


__all__ = [
    "VALID_CATEGORIES",
    "VALID_STATUSES",
    "MAX_TITLE_LEN",
    "MAX_EMAIL_LEN",
    "MAX_IMAGES",
    "MAX_IMAGE_BYTES",
    "ALLOWED_IMAGE_PREFIX",
    "normalize_category",
    "normalize_title",
    "validate_status",
    "validate_email",
    "safe_filename",
]
