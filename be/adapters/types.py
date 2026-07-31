"""Value types crossing the adapter seam.

These are the vendor-neutral shapes every provider speaks in. Amounts are always
integer minor units (agorot / cents) to avoid float drift; ``currency`` is an
ISO-4217 code. Everything here is a frozen dataclass — cheap, hashable, typed, and
importable with zero dependencies beyond the stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Money:
    """A monetary amount in integer minor units plus an ISO-4217 currency code."""

    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount_minor, int):  # guard against float drift
            raise ValueError("amount_minor must be an int (minor units)")
        if not self.currency or len(self.currency) != 3:
            raise ValueError("currency must be a 3-letter ISO-4217 code")


@dataclass(frozen=True)
class CardToken:
    """An opaque, reusable payment token returned by ``tokenize``."""

    value: str


@dataclass(frozen=True)
class PanRef:
    """A one-time reference to raw card data held by the vendor/terminal."""

    value: str


@dataclass(frozen=True)
class AuthResult:
    """Outcome of an authorization hold."""

    auth_id: str
    approved: bool


@dataclass(frozen=True)
class CaptureResult:
    """Outcome of a settled charge (capture, notify-finalize, or external record)."""

    charge_id: str
    amount: Money
    channel: str = "card"


@dataclass(frozen=True)
class RefundResult:
    """Outcome of a refund."""

    refund_id: str
    amount: Money


@dataclass(frozen=True)
class NotifyEvent:
    """A verified (or rejected) provider callback — webhook / iframe notify."""

    provider: str
    event_id: str
    verified: bool
    raw: dict


@dataclass(frozen=True)
class Completion:
    """An LLM completion — the text plus the raw provider payload when available."""

    text: str
    raw: dict | None = None


@dataclass(frozen=True)
class SendResult:
    """Best-effort notification outcome."""

    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class Principal:
    """An authenticated caller: stable uid, verified claims, and venue scoping."""

    uid: str
    claims: dict = field(default_factory=dict)
    venues: list[str] = field(default_factory=list)


__all__ = [
    "Money",
    "CardToken",
    "PanRef",
    "AuthResult",
    "CaptureResult",
    "RefundResult",
    "NotifyEvent",
    "Completion",
    "SendResult",
    "Principal",
]
