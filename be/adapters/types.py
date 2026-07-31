"""Value types crossing the adapter seam.

These are the vendor-neutral shapes every provider speaks in. Amounts are always
integer minor units (agorot / cents) to avoid float drift; ``currency`` is an
ISO-4217 code. Everything here is a frozen dataclass — cheap, hashable, typed, and
importable with zero dependencies beyond the stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


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


class Tender(StrEnum):
    """How the original charge was tendered — selects the refund/reversal path.

    Mirrors the real Tranzila fan-out (be/app/services/tranzila_refund.py +
    tranzila_paybridge.refund): Bit has no card token so it needs its own endpoint;
    the EMV device refund is a separate CGI from the card-REST credit/void; and
    off-platform revenue is reversed in-ledger only.
    """

    CARD_IFRAME = "card_iframe"  # hosted-page online card -> credit_card/create (credit/reversal)
    CARD_EMV = "card_emv"  # PAX A8900 Pay Bridge device -> tranzila_paybridge.refund
    BIT = "bit"  # Bit wallet -> transaction/bit/refund
    EXTERNAL = "external"  # off-platform (Wolt) -> ledger reversal, no vendor call


@dataclass(frozen=True)
class RefundContext:
    """Tender/charge-source context the real refund router needs but the flat
    ``refund(charge_id, amount, idem_key)`` signature could not carry.

    * ``tender`` picks the vendor path (Bit vs card-REST vs EMV device).
    * ``same_day`` selects a pre-settlement void (``reversal``) over a ``credit``.
    * ``original_authnr`` is required by the card-REST credit/void (references the
      original transaction by authorization number, no PAN needed).
    * ``terminal_name`` scopes multi-terminal deployments (optional; provider default).

    All fields default so callers/fakes can build a plain ``RefundContext()``.
    """

    tender: Tender = Tender.CARD_IFRAME
    same_day: bool = False
    original_authnr: str | None = None
    terminal_name: str | None = None


@dataclass(frozen=True)
class DocumentArtifact:
    """A minted fiscal document: its number plus the rendered artifact.

    ``number`` is ``str | int`` — Postgres Z-numbers are ``int``; Tranzila invoice
    numbers are vendor-minted strings. ``content`` is either the rendered document
    text (generic/local render) or a remote locator (Tranzila ``pdf_url`` /
    retrieval key); ``content_type`` disambiguates.
    """

    kind: str
    number: str | int
    content: str
    content_type: str = "text/plain"


@dataclass(frozen=True)
class ImagePart:
    """A single multimodal input: raw bytes plus an EXPLICIT MIME type.

    MIME is never sniffed — the vendor's inlineData needs one MIME per part and
    order-import/form-extract mix ``image/*`` with ``application/pdf`` in one call.
    """

    data: bytes
    mime_type: str


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
    "Tender",
    "RefundContext",
    "DocumentArtifact",
    "ImagePart",
    "NotifyEvent",
    "Completion",
    "SendResult",
    "Principal",
]
