"""``DemoPaymentProvider`` — deterministic, offline fake payment provider.

No network, no credentials. IDs are derived from the idempotency key (or a stable
hash) so the same inputs always yield the same outputs — which is exactly what the
seam-composition e2e test relies on. This is the ``demo`` provider that lets the OSS
core run an order → pay → refund flow with zero external creds. It implements the
required :class:`PaymentProvider` contract plus every optional capability mixin, so
it doubles as the fully-capable reference fake.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace

from be.adapters.errors import ProviderPermanent
from be.adapters.payments.base import (
    PaymentProvider,
    SupportsAuthCapture,
    SupportsExternalRevenue,
    SupportsTokenize,
)
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


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


class DemoPaymentProvider(
    PaymentProvider, SupportsAuthCapture, SupportsTokenize, SupportsExternalRevenue
):
    """Deterministic in-memory payment fake — implements every capability."""

    provider_name = "demo"

    # --- SupportsAuthCapture --------------------------------------------------
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

    # --- SupportsTokenize -----------------------------------------------------
    async def tokenize(self, pan_ref: PanRef) -> CardToken:
        if not pan_ref.value:
            raise ProviderPermanent("tokenize requires a pan_ref")
        return CardToken(value=f"tok_{_digest(pan_ref.value)}")

    # --- SupportsExternalRevenue ----------------------------------------------
    async def record_external(self, channel: str, amount: Money) -> CaptureResult:
        if not channel:
            raise ProviderPermanent("record_external requires a channel")
        return CaptureResult(
            charge_id=f"ext_{_digest(channel, str(amount.amount_minor), amount.currency)}",
            amount=amount,
            channel=channel,
        )

    # --- PaymentProvider (required) -------------------------------------------
    async def refund(
        self, charge_id: str, amount: Money, ctx: RefundContext, idem_key: str
    ) -> RefundResult:
        if not charge_id:
            raise ProviderPermanent("refund requires a charge_id")
        # ctx.tender folds into the id so different tender paths are distinguishable.
        return RefundResult(
            refund_id=f"rfnd_{_digest(charge_id, ctx.tender.value, idem_key)}",
            amount=amount,
        )

    def parse_notify(self, headers: Mapping[str, str], body: bytes | str) -> NotifyEvent:
        if isinstance(body, (bytes, bytearray)):
            body = bytes(body).decode("utf-8")
        try:
            raw = json.loads(body) if body else {}
        except (json.JSONDecodeError, TypeError):
            raw = {"_raw": body}
        # Stash whether a demo signature header was present so the async verify can use it.
        raw = {
            **raw,
            "_demo_signed": bool(
                headers.get("x-demo-signature") or headers.get("X-Demo-Signature")
            ),
        }
        event_id = str(raw.get("event_id") or _digest(json.dumps(raw, sort_keys=True)))
        return NotifyEvent(
            provider=self.provider_name, event_id=event_id, verified=False, raw=raw
        )

    async def verify_notify(self, ev: NotifyEvent) -> NotifyEvent:
        # Deterministic offline "requery": trust the signature flag recorded at parse.
        return replace(ev, verified=bool(ev.raw.get("_demo_signed")))

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


__all__ = ["DemoPaymentProvider"]
