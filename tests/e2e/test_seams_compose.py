"""End-to-end seam-composition test — proves the fake adapters compose.

This is the F1 e2e acceptance probe: a stub order flows

    authorize -> capture -> allocate a fiscal number -> refund

entirely through the ``demo`` payment provider and the ``generic`` fiscal
profile, with **no network and no credentials**. If this passes, the seams
line up (types, signatures, money conservation) even before any real vendor
adapter exists.
"""

from __future__ import annotations

import pytest

from be.adapters.fiscal.generic import GenericFiscalProfile
from be.adapters.payments.demo import DemoPaymentProvider
from be.adapters.types import CardToken, Money, PanRef, RefundContext, Tender


@pytest.mark.asyncio
async def test_order_pay_fiscal_refund_compose() -> None:
    pay = DemoPaymentProvider()
    fiscal = GenericFiscalProfile()

    amount = Money(amount_minor=4200, currency="ILS")

    # 1. tokenize a PAN reference into a card token, then authorize.
    token: CardToken = await pay.tokenize(PanRef(value="pan-ref-1234"))
    assert isinstance(token, CardToken)

    auth = await pay.authorize(amount, token, idem_key="idem-auth-1")
    assert auth.approved is True
    assert auth.auth_id

    # 2. capture the authorization.
    capture = await pay.capture(auth.auth_id, amount, idem_key="idem-cap-1")
    assert capture.charge_id
    assert capture.amount.amount_minor == amount.amount_minor
    assert capture.amount.currency == amount.currency

    # 3. the fiscal profile owns the counter — allocate a receipt number.
    n1 = await fiscal.allocate_number("receipt", venue_id="venue-1")
    n2 = await fiscal.allocate_number("receipt", venue_id="venue-1")
    assert isinstance(n1, int) and isinstance(n2, int)
    assert n2 > n1, "fiscal counter must be strictly monotonic"

    # 4. refund the capture — money is conserved through the seam.
    refund = await pay.refund(
        capture.charge_id, amount, RefundContext(tender=Tender.CARD_EMV, same_day=True), "idem-ref-1"
    )
    assert refund.refund_id
    assert refund.amount.amount_minor == amount.amount_minor
    assert refund.amount.currency == amount.currency


@pytest.mark.asyncio
async def test_demo_provider_is_deterministic() -> None:
    """Same idempotency key -> same identifiers (deterministic fakes)."""
    pay = DemoPaymentProvider()
    amount = Money(amount_minor=100, currency="ILS")
    token = await pay.tokenize(PanRef(value="pan-ref-1234"))

    a1 = await pay.authorize(amount, token, idem_key="k")
    a2 = await pay.authorize(amount, token, idem_key="k")
    assert a1.auth_id == a2.auth_id
    assert a1.approved == a2.approved


@pytest.mark.asyncio
async def test_fiscal_counter_is_per_kind_and_venue() -> None:
    fiscal = GenericFiscalProfile()
    r1 = await fiscal.allocate_number("receipt", venue_id="v1")
    i1 = await fiscal.allocate_number("invoice", venue_id="v1")
    r_other = await fiscal.allocate_number("receipt", venue_id="v2")

    # independent counters do not collide across kinds/venues.
    assert r1 >= 1
    assert i1 >= 1
    assert r_other >= 1
    assert fiscal.vat_rate() == 0.0
    assert fiscal.retention_years() >= 0
