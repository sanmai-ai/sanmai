"""Clock domain service helpers — pure token/TTL/kind logic (no DB, no I/O).

Vendor- and locale-neutral. The session TTL is enforced by comparing ISO-8601
timestamps written from Python (there is no ``now()`` on the portable SQL path);
expiry is therefore evaluated lazily on read rather than by a cron.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

# A clock session is a single-use, short-TTL handoff between the station (mints the
# QR) and the staff phone (redeems it). 60s mirrors the live realtime handoff window.
SESSION_TTL_SECONDS = 60

# Auto-detected at redeem: an open shift today -> clock_out, else clock_in.
CLOCK_IN = "clock_in"
CLOCK_OUT = "clock_out"
VALID_KINDS: tuple[str, ...] = (CLOCK_IN, CLOCK_OUT)

# pending -> used -> completed  (and pending -> expired on TTL / force-close).
STATUS_PENDING = "pending"
STATUS_USED = "used"
STATUS_COMPLETED = "completed"
STATUS_EXPIRED = "expired"
VALID_STATUSES: tuple[str, ...] = (
    STATUS_PENDING,
    STATUS_USED,
    STATUS_COMPLETED,
    STATUS_EXPIRED,
)


def new_token() -> str:
    """Mint an opaque, URL-safe session token — this is the QR payload."""
    return secrets.token_urlsafe(24)


def now() -> datetime:
    """Timezone-aware UTC now (single source, so tests can reason about TTL)."""
    return datetime.now(UTC)


def expiry_for(created_at: datetime) -> datetime:
    """The absolute expiry instant for a session minted at *created_at*."""
    return created_at + timedelta(seconds=SESSION_TTL_SECONDS)


def is_expired(expires_at_iso: str, *, at: datetime | None = None) -> bool:
    """True if *expires_at_iso* is at/before *at* (defaults to :func:`now`).

    Parses ISO-8601 (portable TEXT timestamps); a malformed value is treated as
    expired so a bad row can never be redeemed.
    """
    moment = at or now()
    try:
        expires = datetime.fromisoformat(expires_at_iso)
    except ValueError:
        return True
    if expires.tzinfo is None:  # be lenient about a naive stored value
        expires = expires.replace(tzinfo=UTC)
    return expires <= moment


def qr_payload(token: str) -> str:
    """The value the station encodes into the QR. Kept as a seam so a URL wrapper
    (e.g. ``/staff/?session=<token>``) can be layered on the FE without a BE change."""
    return token


__all__ = [
    "SESSION_TTL_SECONDS",
    "CLOCK_IN",
    "CLOCK_OUT",
    "VALID_KINDS",
    "STATUS_PENDING",
    "STATUS_USED",
    "STATUS_COMPLETED",
    "STATUS_EXPIRED",
    "VALID_STATUSES",
    "new_token",
    "now",
    "expiry_for",
    "is_expired",
    "qr_payload",
]
