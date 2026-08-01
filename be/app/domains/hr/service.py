"""HR domain service helpers — pure identity/validation logic (no DB, no I/O).

Kept vendor- and locale-neutral: phone normalization strips formatting but never injects
a country code (that is a deployment concern, not the core's), email is lowercased/trimmed
for stable identity matching, and time strings are validated/normalized to "HH:MM" so the
storage layer stays on the asyncpg-safe TEXT path.
"""

from __future__ import annotations

import re

_PHONE_SEPARATORS = re.compile(r"[\s\-().]")
_PHONE_VALID = re.compile(r"^\+?\d{7,15}$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

VALID_PAY_TYPES: tuple[str, ...] = ("hourly", "monthly")


def normalize_email(raw: str | None) -> str | None:
    """Lowercase + trim an email for stable identity matching; empty -> ``None``."""
    if raw is None:
        return None
    cleaned = raw.strip().lower()
    return cleaned or None


def normalize_phone(raw: str | None) -> str | None:
    """Strip formatting to a bare (optionally ``+``-prefixed) digit string.

    Locale-neutral: ``00`` international prefixes fold to ``+``, but no country code is
    ever assumed. Returns ``None`` for empty input; raises :class:`ValueError` if what
    remains is not a plausible phone number.
    """
    if raw is None:
        return None
    cleaned = _PHONE_SEPARATORS.sub("", raw.strip())
    if not cleaned:
        return None
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if not _PHONE_VALID.match(cleaned):
        raise ValueError(f"invalid phone number: {raw!r}")
    return cleaned


def normalize_time(raw: str | None) -> str | None:
    """Validate + zero-pad a "H:MM"/"HH:MM" string to "HH:MM"; empty -> ``None``.

    Raises :class:`ValueError` on a malformed or out-of-range time.
    """
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    m = _TIME_RE.match(cleaned)
    if not m:
        raise ValueError(f"time must be HH:MM: {raw!r}")
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        raise ValueError(f"time out of range: {raw!r}")
    return f"{hh:02d}:{mm:02d}"


def validate_pay(pay_type: str, pay_rate: float | None) -> None:
    """Raise :class:`ValueError` for an unknown pay_type or a negative pay_rate."""
    if pay_type not in VALID_PAY_TYPES:
        raise ValueError("pay_type must be 'hourly' or 'monthly'")
    if pay_rate is not None and pay_rate < 0:
        raise ValueError("pay_rate can't be negative")


__all__ = [
    "VALID_PAY_TYPES",
    "normalize_email",
    "normalize_phone",
    "normalize_time",
    "validate_pay",
]
