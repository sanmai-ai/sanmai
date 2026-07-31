"""``PaymentProvider`` — the frozen payment seam.

Modelled from the real Tranzila iframe/notify, EMV Pay Bridge, and Wolt flows.
Only the settlement/refund leg every vendor MUST speak is required; auth-hold,
tokenize, and external-revenue booking are OPTIONAL capabilities (mixins) because
hosted-checkout/EMV vendors (Tranzila) have no server-side delegate for them.

Notify is a two-step handshake:
* ``parse_notify``  — SYNC, pure structural parse of an inbound callback; the
  result is ``verified=False`` (Tranzila's notify is unsigned form-body).
* ``verify_notify`` — ASYNC, authenticates the parsed event out-of-band (the real
  Tranzila Reports-API requery) and returns a ``verified=True`` copy.
* ``finalize_from_notify`` — ASYNC, turns a verified event into a settled charge.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

from be.adapters.types import (
    AuthResult,
    CaptureResult,
    CardToken,
    Money,
    NotifyEvent,
    PanRef,
    RefundContext,
    RefundResult,
)


class PaymentProvider(ABC):
    """Vendor-neutral payment contract — the minimum every provider must satisfy.

    Implementations raise ``AdapterError`` subclasses. Optional capabilities live in
    the ``Supports*`` mixins below; probe with ``isinstance(prov, SupportsAuthCapture)``.
    """

    @abstractmethod
    async def refund(
        self, charge_id: str, amount: Money, ctx: RefundContext, idem_key: str
    ) -> RefundResult:
        """Refund all or part of a settled charge. ``ctx`` selects the tender path."""

    @abstractmethod
    def parse_notify(self, headers: Mapping[str, str], body: bytes | str) -> NotifyEvent:
        """Structurally parse an inbound callback (SYNC, pure). ``verified`` is False."""

    @abstractmethod
    async def verify_notify(self, ev: NotifyEvent) -> NotifyEvent:
        """Authenticate a parsed event out-of-band; return a verified copy (ASYNC)."""

    @abstractmethod
    async def finalize_from_notify(self, ev: NotifyEvent, idem_key: str) -> CaptureResult:
        """Turn a verified notify event into a settled charge."""


class SupportsCapture(ABC):
    """Optional: settle a charge in one blocking step (EMV charge-and-settle).

    No separate authorize — Tranzila has none (card auth happens in the browser
    iframe hosted page; the EMV Pay Bridge charges-and-settles the physical pinpad
    in one blocking POST). Providers with a real auth-hold implement the richer
    :class:`SupportsAuthCapture` instead, which inherits this capability.
    """

    @abstractmethod
    async def capture(self, auth_id: str, amount: Money, idem_key: str) -> CaptureResult:
        """Settle a charge (EMV: charge-and-settle in one blocking step).

        For a charge-and-settle provider ``auth_id`` is the caller's idempotency
        anchor (e.g. the order id), not a prior authorization handle.
        """


class SupportsAuthCapture(SupportsCapture):
    """Optional: an auth-hold + capture split (generic gateways with a real hold).

    Inherits :class:`SupportsCapture` so ``isinstance(prov, SupportsCapture)`` is true
    for both the split and the charge-and-settle case.
    """

    @abstractmethod
    async def authorize(self, amount: Money, token: CardToken, idem_key: str) -> AuthResult:
        """Place an authorization hold for ``amount`` against ``token``."""


class SupportsTokenize(ABC):
    """Optional: mint a reusable card token from a one-time PAN reference."""

    @abstractmethod
    async def tokenize(self, pan_ref: PanRef) -> CardToken:
        """Exchange a one-time PAN reference for a reusable card token."""


class SupportsExternalRevenue(ABC):
    """Optional: book revenue collected off-platform (e.g. Wolt) with no card charge."""

    @abstractmethod
    async def record_external(self, channel: str, amount: Money) -> CaptureResult:
        """Book off-platform revenue so the ledger stays whole."""


__all__ = [
    "PaymentProvider",
    "SupportsCapture",
    "SupportsAuthCapture",
    "SupportsTokenize",
    "SupportsExternalRevenue",
]
