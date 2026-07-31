"""``DemoPaymentProvider`` — deterministic, offline fake payment provider.

No network, no credentials. IDs are derived from the idempotency key (or a stable
hash) so the same inputs always yield the same outputs — which is exactly what the
seam-composition e2e test relies on. This is the ``demo`` provider that lets the OSS
core run an order → pay → refund flow with zero external creds.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from be.adapters.errors import ProviderPermanent
from be.adapters.payments.base import PaymentProvider
from be.adapters.types import (
    AuthResult,
    CaptureResult,
    CardToken,
    Money,
    NotifyEvent,
    PanRef,
    RefundResult,
)


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


class DemoPaymentProvider(PaymentProvider):
    """Deterministic in-memory payment fake."""

    provider_name = "demo"

    async def authorize(self, amount: Money, token: CardToken, idem_key: str) -> AuthResult:
        if amount.amount_minor <= 0:
            raise ProviderPermanent("authorize amount must be positive")
        return AuthResult(auth_id=f"auth_{_digest(idem_key, token.value)}", approved=True)

    async def capture(self, auth_id: str, amount: Money, idem_key: str) -> CaptureResult:
        if not auth_id:
            raise ProviderPermanent("capture requires an auth_id")
        return CaptureResult(
            charge_id=f"chg_{_digest(auth_id, idem_key)}",
            amount=amount,
            channel="card",
        )

    async def refund(self, charge_id: str, amount: Money, idem_key: str) -> RefundResult:
        if not charge_id:
            raise ProviderPermanent("refund requires a charge_id")
        return RefundResult(refund_id=f"rfnd_{_digest(charge_id, idem_key)}", amount=amount)

    async def tokenize(self, pan_ref: PanRef) -> CardToken:
        if not pan_ref.value:
            raise ProviderPermanent("tokenize requires a pan_ref")
        return CardToken(value=f"tok_{_digest(pan_ref.value)}")

    def verify_notify(self, headers: Mapping[str, str], body: bytes | str) -> NotifyEvent:
        raw: dict
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8")
        try:
            raw = json.loads(body) if body else {}
        except (json.JSONDecodeError, TypeError):
            raw = {"_raw": body}
        # The demo provider "verifies" any callback that carries a signature header.
        verified = bool(headers.get("x-demo-signature") or headers.get("X-Demo-Signature"))
        event_id = str(raw.get("event_id") or _digest(json.dumps(raw, sort_keys=True)))
        return NotifyEvent(
            provider=self.provider_name,
            event_id=event_id,
            verified=verified,
            raw=raw,
        )

    async def finalize_from_notify(self, ev: NotifyEvent, idem_key: str) -> CaptureResult:
        if not ev.verified:
            raise ProviderPermanent("cannot finalize an unverified notify event")
        amount_minor = int(ev.raw.get("amount_minor", 0))
        currency = str(ev.raw.get("currency", "ILS"))
        return CaptureResult(
            charge_id=f"chg_{_digest(ev.event_id, idem_key)}",
            amount=Money(amount_minor=amount_minor, currency=currency),
            channel="card",
        )

    async def record_external(self, channel: str, amount: Money) -> CaptureResult:
        if not channel:
            raise ProviderPermanent("record_external requires a channel")
        return CaptureResult(
            charge_id=f"ext_{_digest(channel, str(amount.amount_minor), amount.currency)}",
            amount=amount,
            channel=channel,
        )


__all__ = ["DemoPaymentProvider"]
