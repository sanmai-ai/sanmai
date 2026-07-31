"""Unit tests for the real TranzilaPaymentProvider — httpx MOCKED, no network/creds.

Covers: EMV capture (charge-and-settle) success + decline + infra-transient; refund tender
routing (EMV device credit with fresh uniqueid / Bit / iframe card credit / EXTERNAL reject);
sync parse_notify + async verify_notify requery (approved / amount-mismatch / network); and
finalize_from_notify gating on verification. Uses respx to intercept httpx.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from be.adapters.errors import ProviderConfigError, ProviderPermanent, ProviderTransient
from be.adapters.payments.base import PaymentProvider, SupportsCapture
from be.adapters.payments.tranzila import (
    BIT_REFUND_URL,
    CARD_TXN_URL,
    DEFAULT_PAYBRIDGE_URL,
    REPORT_URL,
    TranzilaPaymentProvider,
)
from be.adapters.types import Money, NotifyEvent, RefundContext, Tender

_ILS = "ILS"


def _prov() -> TranzilaPaymentProvider:
    return TranzilaPaymentProvider(
        api_public_key="pub",
        api_secret_key="sec",
        terminal_name="testterm",
        emv_pos_id="314",
    )


def _money(minor: int) -> Money:
    return Money(amount_minor=minor, currency=_ILS)


# --------------------------------------------------------------- import + construction


def test_import_is_credential_free() -> None:
    import importlib

    import be.adapters.payments.tranzila as mod

    importlib.reload(mod)  # importing must not need creds


def test_is_payment_provider_and_supports_capture() -> None:
    p = _prov()
    assert isinstance(p, PaymentProvider)
    assert isinstance(p, SupportsCapture)


def test_missing_keys_raise_config_error() -> None:
    with pytest.raises(ProviderConfigError):
        TranzilaPaymentProvider(api_public_key="", api_secret_key="s", terminal_name="t")
    with pytest.raises(ProviderConfigError):
        TranzilaPaymentProvider(api_public_key="p", api_secret_key="s", terminal_name="")


def test_signed_headers_are_reproducible_hmac() -> None:
    import hashlib
    import hmac

    p = _prov()
    h = p._signed_headers()
    assert h["X-tranzila-api-app-key"] == "pub"
    assert len(h["X-tranzila-api-nonce"]) == 80
    expected = hmac.new(
        key=("sec" + h["X-tranzila-api-request-time"] + h["X-tranzila-api-nonce"]).encode(),
        msg=b"pub",
        digestmod=hashlib.sha256,
    ).hexdigest()
    assert h["X-tranzila-api-access-token"] == expected


# ------------------------------------------------------------------------- EMV capture


@respx.mock
async def test_capture_emv_success() -> None:
    route = respx.post(DEFAULT_PAYBRIDGE_URL).mock(
        return_value=httpx.Response(
            200, json={"index": "555", "token": "tk", "transaction_result": {"statusCode": 0}}
        )
    )
    cap = await _prov().capture("order-9", _money(1000), idem_key="k")
    assert cap.charge_id == "555" and cap.amount == _money(1000) and cap.channel == "card"
    body = json.loads(route.calls.last.request.content)
    assert body["tranmode"] == "A"
    assert body["uniqueid"] == "order-9"  # order id is the idempotency anchor
    assert body["sum"] == "10.00"


@respx.mock
async def test_capture_decline_is_permanent() -> None:
    respx.post(DEFAULT_PAYBRIDGE_URL).mock(
        return_value=httpx.Response(
            200, json={"transaction_result": {"statusCode": -1, "statusMessage": "declined"}}
        )
    )
    with pytest.raises(ProviderPermanent):
        await _prov().capture("o1", _money(500), idem_key="k")


@respx.mock
async def test_capture_network_is_transient() -> None:
    respx.post(DEFAULT_PAYBRIDGE_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ProviderTransient):
        await _prov().capture("o1", _money(500), idem_key="k")


@respx.mock
async def test_capture_missing_transaction_result_is_transient() -> None:
    respx.post(DEFAULT_PAYBRIDGE_URL).mock(return_value=httpx.Response(200, json={"index": "1"}))
    with pytest.raises(ProviderTransient):
        await _prov().capture("o1", _money(500), idem_key="k")


# ------------------------------------------------------------------- refund tender routing


@respx.mock
async def test_refund_emv_uses_fresh_uniqueid_and_credit_mode() -> None:
    route = respx.post(DEFAULT_PAYBRIDGE_URL).mock(
        return_value=httpx.Response(200, json={"index": "R1", "transaction_result": {"statusCode": 0}})
    )
    res = await _prov().refund(
        "chg42", _money(2500), RefundContext(tender=Tender.CARD_EMV), idem_key="k"
    )
    assert res.refund_id == "R1" and res.amount == _money(2500)
    body = json.loads(route.calls.last.request.content)
    assert body["tranmode"] == "C"
    assert body["uniqueid"].startswith("chg42-r")  # fresh standalone uniqueid, not charge index


@respx.mock
async def test_refund_bit_hits_bit_endpoint() -> None:
    route = respx.post(BIT_REFUND_URL).mock(
        return_value=httpx.Response(
            200, json={"error_code": 0, "transaction_result": {"index": "B7"}}
        )
    )
    res = await _prov().refund("12345", _money(999), RefundContext(tender=Tender.BIT), idem_key="k")
    assert res.refund_id == "B7"
    body = json.loads(route.calls.last.request.content)
    assert body["transaction_id"] == 12345
    assert body["amount"] == 9.99


@respx.mock
async def test_refund_card_iframe_credit() -> None:
    route = respx.post(CARD_TXN_URL).mock(
        return_value=httpx.Response(
            200, json={"error_code": 0, "transaction_result": {"index": "C3"}}
        )
    )
    res = await _prov().refund(
        "778",
        _money(4200),
        RefundContext(tender=Tender.CARD_IFRAME, original_authnr="9911", terminal_name="other"),
        idem_key="k",
    )
    assert res.refund_id == "C3"
    body = json.loads(route.calls.last.request.content)
    assert body["txn_type"] == "credit"
    assert body["reference_txn_id"] == 778
    assert body["authorization_number"] == "9911"
    assert body["terminal_name"] == "other"  # ctx overrides provider default


@respx.mock
async def test_refund_card_rejected_is_permanent() -> None:
    respx.post(CARD_TXN_URL).mock(
        return_value=httpx.Response(200, json={"error_code": 20004, "message": "bad schema"})
    )
    with pytest.raises(ProviderPermanent):
        await _prov().refund("778", _money(100), RefundContext(tender=Tender.CARD_IFRAME), "k")


async def test_refund_card_bad_index_is_permanent() -> None:
    with pytest.raises(ProviderPermanent):
        await _prov().refund("notanumber", _money(100), RefundContext(tender=Tender.BIT), "k")


async def test_refund_external_is_permanent() -> None:
    with pytest.raises(ProviderPermanent):
        await _prov().refund("x", _money(100), RefundContext(tender=Tender.EXTERNAL), "k")


@respx.mock
async def test_refund_card_network_is_transient() -> None:
    respx.post(CARD_TXN_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ProviderTransient):
        await _prov().refund("778", _money(100), RefundContext(tender=Tender.CARD_IFRAME), "k")


# ------------------------------------------------------------------------ notify handshake


def test_parse_notify_is_sync_unverified() -> None:
    ev = _prov().parse_notify({}, "index=8801&sum=42.00&Response=000")
    assert isinstance(ev, NotifyEvent)
    assert ev.provider == "tranzila" and ev.event_id == "8801" and ev.verified is False
    assert ev.raw["sum"] == "42.00"


def test_parse_notify_accepts_bytes() -> None:
    ev = _prov().parse_notify({}, b"TranzactionId=99&Amount=10")
    assert ev.event_id == "99"


@respx.mock
async def test_verify_notify_approved() -> None:
    respx.post(REPORT_URL).mock(
        return_value=httpx.Response(
            200, json={"processor_response_code": "000", "transtatus": 2, "amount": 4200, "index": 8801}
        )
    )
    ev = _prov().parse_notify({}, "index=8801&sum=42.00")
    verified = await _prov().verify_notify(ev)
    assert verified.verified is True and verified.event_id == "8801"


@respx.mock
async def test_verify_notify_amount_mismatch_not_verified() -> None:
    respx.post(REPORT_URL).mock(
        return_value=httpx.Response(
            200, json={"processor_response_code": "000", "amount": 9999, "index": 8801}
        )
    )
    ev = _prov().parse_notify({}, "index=8801&sum=42.00")
    assert (await _prov().verify_notify(ev)).verified is False


@respx.mock
async def test_verify_notify_declined_code_not_verified() -> None:
    respx.post(REPORT_URL).mock(
        return_value=httpx.Response(200, json={"processor_response_code": "033", "amount": 4200})
    )
    ev = _prov().parse_notify({}, "index=8801&sum=42.00")
    assert (await _prov().verify_notify(ev)).verified is False


@respx.mock
async def test_verify_notify_network_is_transient() -> None:
    respx.post(REPORT_URL).mock(side_effect=httpx.ConnectError("blip"))
    ev = _prov().parse_notify({}, "index=8801&sum=42.00")
    with pytest.raises(ProviderTransient):
        await _prov().verify_notify(ev)


async def test_verify_notify_non_numeric_index_not_verified() -> None:
    ev = NotifyEvent(provider="tranzila", event_id="", verified=False, raw={"sum": "1"})
    assert (await _prov().verify_notify(ev)).verified is False


async def test_finalize_requires_verified() -> None:
    ev = NotifyEvent(provider="tranzila", event_id="1", verified=False, raw={"sum": "10"})
    with pytest.raises(ProviderPermanent):
        await _prov().finalize_from_notify(ev, idem_key="k")


async def test_finalize_returns_capture() -> None:
    ev = NotifyEvent(provider="tranzila", event_id="8801", verified=True, raw={"sum": "42.00"})
    cap = await _prov().finalize_from_notify(ev, idem_key="k")
    assert cap.charge_id == "8801"
    assert cap.amount == _money(4200) and cap.channel == "card"
