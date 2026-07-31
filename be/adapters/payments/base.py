"""``PaymentProvider`` — the frozen payment seam.

Modelled from the real Tranzila iframe/notify, EMV Pay Bridge, and Wolt flows rather
than generic e-commerce. Two things are first-class that generic gateways omit:

* **Async settlement** — a charge may complete out-of-band via an iframe/webhook
  callback; ``verify_notify`` authenticates the callback and ``finalize_from_notify``
  turns a verified event into a settled :class:`CaptureResult`.
* **Record-revenue-without-charging** — ``record_external`` books revenue that was
  collected by a third party (e.g. Wolt) so the ledger stays whole.

All network-bound methods are ``async``; ``verify_notify`` is sync because it is pure
signature/HMAC math over the request headers and body.
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
    RefundResult,
)


class PaymentProvider(ABC):
    """Vendor-neutral payment contract. Implementations raise ``AdapterError``."""

    @abstractmethod
    async def authorize(self, amount: Money, token: CardToken, idem_key: str) -> AuthResult:
        """Place an authorization hold for ``amount`` against ``token``."""

    @abstractmethod
    async def capture(self, auth_id: str, amount: Money, idem_key: str) -> CaptureResult:
        """Settle a prior authorization."""

    @abstractmethod
    async def refund(self, charge_id: str, amount: Money, idem_key: str) -> RefundResult:
        """Refund all or part of a settled charge."""

    @abstractmethod
    async def tokenize(self, pan_ref: PanRef) -> CardToken:
        """Exchange a one-time PAN reference for a reusable card token."""

    @abstractmethod
    def verify_notify(self, headers: Mapping[str, str], body: bytes | str) -> NotifyEvent:
        """Authenticate an inbound webhook/iframe callback (pure, sync)."""

    @abstractmethod
    async def finalize_from_notify(self, ev: NotifyEvent, idem_key: str) -> CaptureResult:
        """Turn a verified notify event into a settled charge."""

    @abstractmethod
    async def record_external(self, channel: str, amount: Money) -> CaptureResult:
        """Book revenue collected off-platform (e.g. Wolt) with no card charge."""


__all__ = ["PaymentProvider"]
