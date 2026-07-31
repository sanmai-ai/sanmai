"""Unit tests for the adapter seam — every fake, every method, zero external creds.

``asyncio_mode = "auto"`` (pyproject) means ``async def test_*`` runs without an
explicit marker. These tests are the acceptance probe for the frozen contracts.
"""

from __future__ import annotations

import json

import pytest

from be.adapters.errors import ProviderPermanent
from be.adapters.fiscal.base import FiscalProfile
from be.adapters.fiscal.generic import GenericFiscalProfile
from be.adapters.identity.base import IdentityProvider
from be.adapters.identity.static import StaticIdentityProvider
from be.adapters.llm.base import LLMProvider
from be.adapters.llm.echo import EchoLLMProvider
from be.adapters.notify.base import Notifier
from be.adapters.notify.noop import NoopNotifier
from be.adapters.payments.base import PaymentProvider
from be.adapters.payments.demo import DemoPaymentProvider
from be.adapters.storage.base import Storage
from be.adapters.storage.local import LocalStorage
from be.adapters.types import (
    CardToken,
    Money,
    NotifyEvent,
    PanRef,
    Principal,
)

# --------------------------------------------------------------------------- types


def test_money_rejects_float_and_bad_currency() -> None:
    m = Money(amount_minor=1500, currency="ILS")
    assert m.amount_minor == 1500 and m.currency == "ILS"
    with pytest.raises(ValueError):
        Money(amount_minor=1.5, currency="ILS")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Money(amount_minor=100, currency="SHEKEL")


def test_principal_defaults() -> None:
    p = Principal(uid="u1")
    assert p.claims == {} and p.venues == []


# ------------------------------------------------------------------------- payments


async def test_demo_payment_full_flow() -> None:
    prov = DemoPaymentProvider()
    assert isinstance(prov, PaymentProvider)
    money = Money(amount_minor=2500, currency="ILS")

    token = await prov.tokenize(PanRef(value="4111-xxxx"))
    assert isinstance(token, CardToken) and token.value.startswith("tok_")

    auth = await prov.authorize(money, token, idem_key="k1")
    assert auth.approved and auth.auth_id.startswith("auth_")

    cap = await prov.capture(auth.auth_id, money, idem_key="k1")
    assert cap.charge_id.startswith("chg_") and cap.amount == money and cap.channel == "card"

    refund = await prov.refund(cap.charge_id, money, idem_key="k1")
    assert refund.refund_id.startswith("rfnd_") and refund.amount == money


async def test_demo_payment_deterministic() -> None:
    prov = DemoPaymentProvider()
    money = Money(amount_minor=100, currency="ILS")
    tok = CardToken(value="tok_fixed")
    a1 = await prov.authorize(money, tok, idem_key="same")
    a2 = await prov.authorize(money, tok, idem_key="same")
    assert a1.auth_id == a2.auth_id


async def test_demo_payment_rejects_bad_input() -> None:
    prov = DemoPaymentProvider()
    with pytest.raises(ProviderPermanent):
        await prov.authorize(Money(amount_minor=0, currency="ILS"), CardToken("t"), "k")
    with pytest.raises(ProviderPermanent):
        await prov.tokenize(PanRef(value=""))


def test_demo_verify_notify() -> None:
    prov = DemoPaymentProvider()
    body = json.dumps({"event_id": "evt_9", "amount_minor": 500, "currency": "ILS"})

    unsigned = prov.verify_notify({}, body)
    assert isinstance(unsigned, NotifyEvent) and unsigned.verified is False

    signed = prov.verify_notify({"x-demo-signature": "sig"}, body.encode("utf-8"))
    assert signed.verified is True and signed.event_id == "evt_9"


async def test_demo_finalize_and_external() -> None:
    prov = DemoPaymentProvider()
    ev = prov.verify_notify(
        {"x-demo-signature": "sig"},
        json.dumps({"event_id": "e1", "amount_minor": 700, "currency": "ILS"}),
    )
    cap = await prov.finalize_from_notify(ev, idem_key="k")
    assert cap.charge_id.startswith("chg_") and cap.amount.amount_minor == 700

    bad = NotifyEvent(provider="demo", event_id="x", verified=False, raw={})
    with pytest.raises(ProviderPermanent):
        await prov.finalize_from_notify(bad, idem_key="k")

    ext = await prov.record_external("wolt", Money(amount_minor=1234, currency="ILS"))
    assert ext.channel == "wolt" and ext.charge_id.startswith("ext_")


# ------------------------------------------------------------------------------ llm


async def test_echo_llm() -> None:
    llm = EchoLLMProvider()
    assert isinstance(llm, LLMProvider)
    out = await llm.complete("hello", images=[b"img"], schema={"type": "object"})
    assert out.text == "hello"
    assert out.raw is not None
    assert out.raw["image_count"] == 1 and out.raw["schema"] == {"type": "object"}


# --------------------------------------------------------------------------- fiscal


def test_generic_fiscal() -> None:
    fp = GenericFiscalProfile()
    assert isinstance(fp, FiscalProfile)
    assert fp.vat_rate() == 0.0 and fp.retention_years() == 7

    # monotonic per (kind, venue)
    assert fp.allocate_number("receipt", "v1") == 1
    assert fp.allocate_number("receipt", "v1") == 2
    assert fp.allocate_number("receipt", "v2") == 1
    assert fp.allocate_number("invoice", "v1") == 1

    order = {"id": "o1", "items": [{"name": "gyoza", "qty": 2, "price_minor": 300}], "total_minor": 600}
    receipt = fp.render_receipt(order)
    invoice = fp.render_invoice(order)
    assert "RECEIPT" in receipt and "gyoza" in receipt and "TOTAL: 600" in receipt
    assert "TAX INVOICE" in invoice


# --------------------------------------------------------------------------- notify


async def test_noop_notifier() -> None:
    n = NoopNotifier()
    assert isinstance(n, Notifier)
    res = await n.send("telegram_approval", {"text": "hi"})
    assert res.ok is True
    assert n.sent == [("telegram_approval", {"text": "hi"})]


# -------------------------------------------------------------------------- storage


async def test_local_storage_roundtrip() -> None:
    s = LocalStorage()
    assert isinstance(s, Storage)
    key = await s.put("docs/report.pdf", b"%PDF-bytes")
    assert key == "docs/report.pdf"
    assert await s.get("docs/report.pdf") == b"%PDF-bytes"
    assert s.signed_url("docs/report.pdf", ttl=60).startswith("file://")
    with pytest.raises(ProviderPermanent):
        await s.get("missing/key")


# -------------------------------------------------------------------------- identity


def test_static_identity_json_and_b64() -> None:
    import base64

    idp = StaticIdentityProvider()
    assert isinstance(idp, IdentityProvider)

    raw = json.dumps({"uid": "u42", "claims": {"role": "admin"}, "venues": ["v1", "v2"]})
    p = idp.verify_token(raw)
    assert p.uid == "u42" and p.claims["role"] == "admin" and p.venues == ["v1", "v2"]

    b64 = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8").rstrip("=")
    p2 = idp.verify_token(b64)
    assert p2.uid == "u42"

    with pytest.raises(ProviderPermanent):
        idp.verify_token("")
    with pytest.raises(ProviderPermanent):
        idp.verify_token(json.dumps({"claims": {}}))
