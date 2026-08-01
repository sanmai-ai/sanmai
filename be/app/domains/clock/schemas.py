"""Pydantic request models for the clock domain.

Create-style models carry sensible defaults (house convention: a builder FE can
``POST {}``); the router validates the genuinely required fields. Nothing here is
brand/locale-specific. The redeeming employee is NEVER named in a body — it is
resolved from the verified token — so no identity field exists on purpose.
"""

from __future__ import annotations

from pydantic import BaseModel


class RedeemSession(BaseModel):
    # The QR payload the phone scanned. The employee is resolved from the verified
    # token, never from this body (self-only clocking).
    token: str = ""


__all__ = ["RedeemSession"]
