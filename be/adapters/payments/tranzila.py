"""``TranzilaPaymentProvider`` — real Tranzila money spine over ``httpx``.

Self-contained: talks to the Tranzila REST API, Reports API, and the EMV Pay Bridge
CGI directly (no ``app.services.*``). Implements the required :class:`PaymentProvider`
contract plus the :class:`SupportsCapture` capability (EMV charge-and-settle) — Tranzila
has **no** authorize-hold (card auth happens in the browser iframe hosted page), no
server-side tokenize (tokens arrive from the iframe / Pay Bridge, never minted), and no
off-platform revenue booking, so those mixins are intentionally NOT implemented.

Notify is the two-step handshake the ABC prescribes:

* ``parse_notify``  — SYNC, pure. Tranzila's notify is FORM-encoded and UNSIGNED, so the
  parse yields ``verified=False``.
* ``verify_notify`` — ASYNC. The real authentication: an HMAC-signed Reports-API requery
  gated on ``processor_response_code == "000"`` + an exact-amount match.
* ``finalize_from_notify`` — ASYNC. Turns a verified event into a settled charge.

Refunds fan out on :class:`RefundContext.tender` (the reason ``ctx`` exists): the EMV
device credit is a standalone Pay Bridge CGI, Bit has its own endpoint, and iframe card
charges go through the card-REST credit path.

Importing this module needs no creds: ``httpx`` is cred-free; the HMAC keys are validated
at construction (fail-loud at wiring), never at import.
"""

from __future__ import annotations

import binascii
import hashlib
import hmac
import secrets
import time
from collections.abc import Mapping
from dataclasses import replace
from urllib.parse import parse_qsl

import httpx

from be.adapters.errors import ProviderConfigError, ProviderPermanent, ProviderTransient
from be.adapters.payments.base import PaymentProvider, SupportsCapture
from be.adapters.types import (
    CaptureResult,
    Money,
    NotifyEvent,
    RefundContext,
    RefundResult,
    Tender,
)

# --- documented vendor endpoints (not secrets) -------------------------------------
REPORT_URL = "https://report.tranzila.com/v1/transaction"
CARD_TXN_URL = "https://api.tranzila.com/v1/transaction/credit_card/create"
BIT_REFUND_URL = "https://api.tranzila.com/v1/transaction/bit/refund"
DEFAULT_PAYBRIDGE_URL = "https://pinpad.tranzila.com/pinpad/PinpadServiceHandler.ashx"

_ILS = "ILS"
_ILS_CURRENCY_CODE = "376"  # numeric ISO code required by the Pay Bridge CGI
_PAYBRIDGE_TIMEOUT_S = 120.0  # pinpad blocks on the customer tap/insert + PIN
_REST_TIMEOUT_S = 30.0
_REPORT_TIMEOUT_S = 15.0
_SUM_TOLERANCE_AGOROT = 1  # ₪0.01 amount-match tolerance on requery


def _minor_to_major(amount: Money) -> float:
    """agorot (int) -> major ILS float, as every Tranzila call expects."""
    return round(amount.amount_minor / 100.0, 2)


def _extract_txn(data: object, transaction_index: int) -> dict | None:
    """Unwrap the tolerant Reports-API envelope; match a list on ``index``."""
    if isinstance(data, dict):
        if "transtatus" in data or "processor_response_code" in data:
            return data
        for key in ("transaction", "data", "result"):
            inner = data.get(key)
            if isinstance(inner, dict):
                return inner
            if isinstance(inner, list):
                data = inner
                break
        else:
            if isinstance(data.get("transactions"), list):
                data = data["transactions"]
            else:
                return None
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict) and int(row.get("index", -1) or -1) == transaction_index:
                return row
        return data[0] if data and isinstance(data[0], dict) else None
    return None


class TranzilaPaymentProvider(PaymentProvider, SupportsCapture):
    """Real Tranzila adapter — REST credit/bit refund, Reports-API requery, EMV Pay Bridge."""

    provider_name = "tranzila"

    def __init__(
        self,
        *,
        api_public_key: str,
        api_secret_key: str,
        terminal_name: str,
        emv_pos_id: str | None = None,
        paybridge_url: str = DEFAULT_PAYBRIDGE_URL,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_public_key or not api_secret_key:
            raise ProviderConfigError(
                "tranzila requires api_public_key and api_secret_key"
            )
        if not terminal_name:
            raise ProviderConfigError("tranzila requires a terminal_name")
        self._public_key = api_public_key
        self._secret_key = api_secret_key
        self._terminal_name = terminal_name
        self._emv_pos_id = emv_pos_id
        self._paybridge_url = paybridge_url
        # Injected client lets tests mock the transport without touching a network.
        self._client = http_client

    # --- HMAC auth (docs.tranzila.com app-key scheme) --------------------------

    def _signed_headers(self) -> dict[str, str]:
        request_time = str(int(time.time()))
        nonce = binascii.hexlify(secrets.token_bytes(40)).decode("utf-8")  # 80 hex
        access_token = hmac.new(
            key=(self._secret_key + request_time + nonce).encode("utf-8"),
            msg=self._public_key.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()
        return {
            "X-tranzila-api-app-key": self._public_key,
            "X-tranzila-api-request-time": request_time,
            "X-tranzila-api-nonce": nonce,
            "X-tranzila-api-access-token": access_token,
            "Content-Type": "application/json",
        }

    async def _post(
        self, url: str, body: dict, *, headers: dict[str, str], timeout: float
    ) -> dict:
        """POST JSON, returning the parsed body. Network/HTTP failures propagate."""
        if self._client is not None:
            resp = await self._client.post(url, headers=headers, json=body, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            return resp.json()

    async def _post_signed(self, url: str, body: dict, *, timeout: float) -> dict:
        return await self._post(url, body, headers=self._signed_headers(), timeout=timeout)

    # --- capture: EMV Pay Bridge charge-and-settle -----------------------------

    async def capture(self, auth_id: str, amount: Money, idem_key: str) -> CaptureResult:
        """Charge the physical EMV terminal (tranmode ``"A"``). ``auth_id`` is the order
        id and the Pay Bridge ``uniqueid`` (the real idempotency anchor)."""
        res = await self._paybridge(tranmode="A", uniqueid=auth_id, amount=amount)
        if res["statusCode"] != 0:
            raise ProviderPermanent(
                f"card declined: {res['statusMessage']} (code={res['statusCode']})"
            )
        return CaptureResult(charge_id=res["index"], amount=amount, channel="card")

    async def _paybridge(self, *, tranmode: str, uniqueid: str, amount: Money) -> dict:
        """Shared EMV transport (charge/refund). Infra failure -> ProviderTransient;
        a decline is a normal parsed return with ``statusCode != 0``."""
        body = {
            "supplier": self._terminal_name,
            "pos_id": str(self._emv_pos_id or ""),
            "sum": f"{_minor_to_major(amount):.2f}",
            "currency": _ILS_CURRENCY_CODE,
            "tranmode": tranmode,
            "cred_type": "1",
            "uniqueid": uniqueid,
        }
        try:
            data = await self._post(
                self._paybridge_url,
                body,
                headers={"Content-Type": "application/json"},  # Pay Bridge takes NO auth
                timeout=_PAYBRIDGE_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:  # network/timeout/HTTP -> retryable
            raise ProviderTransient(f"Pay Bridge {tranmode} request failed: {exc}") from exc

        txn = (data or {}).get("transaction_result")
        if not isinstance(txn, dict) or "statusCode" not in txn:
            raise ProviderTransient(
                f"Pay Bridge returned no transaction_result (pos_id may be unmapped): {data!r}"
            )
        try:
            status_code = int(txn["statusCode"])
        except (TypeError, ValueError) as exc:
            raise ProviderTransient(
                f"Pay Bridge statusCode not an int: {txn.get('statusCode')!r}"
            ) from exc
        return {
            "statusCode": status_code,
            "statusMessage": str(txn.get("statusMessage") or ""),
            # index/token sit at the TOP level, not inside transaction_result.
            "index": str(data.get("index") or ""),
        }

    # --- refund: route on the tender -------------------------------------------

    async def refund(
        self, charge_id: str, amount: Money, ctx: RefundContext, idem_key: str
    ) -> RefundResult:
        """Refund a settled charge. ``ctx.tender`` selects the vendor path."""
        if ctx.tender is Tender.EXTERNAL:
            raise ProviderPermanent(
                "external tender has no vendor refund — reverse in the ledger above the seam"
            )
        if ctx.tender is Tender.CARD_EMV:
            return await self._refund_emv(charge_id, amount)
        if ctx.tender is Tender.BIT:
            return await self._refund_bit(charge_id, amount)
        return await self._refund_card(charge_id, amount, ctx)

    async def _refund_emv(self, charge_id: str, amount: Money) -> RefundResult:
        # Device credit is STANDALONE — a fresh uniqueid (never the charge index), since
        # the online API rejects EMV refunds (reversal 23001).
        fresh_uniqueid = f"{charge_id}-r{int(time.time())}"
        res = await self._paybridge(tranmode="C", uniqueid=fresh_uniqueid, amount=amount)
        if res["statusCode"] != 0:
            raise ProviderPermanent(
                f"EMV refund declined: {res['statusMessage']} (code={res['statusCode']})"
            )
        return RefundResult(refund_id=res["index"], amount=amount)

    async def _refund_bit(self, charge_id: str, amount: Money) -> RefundResult:
        try:
            transaction_id = int(charge_id)
        except (TypeError, ValueError) as exc:
            raise ProviderPermanent(
                f"bit refund transaction_id must be numeric, got {charge_id!r}"
            ) from exc
        body = {
            "terminal_name": self._terminal_name,
            "transaction_id": transaction_id,
            "amount": round(_minor_to_major(amount), 2),
        }
        try:
            data = await self._post_signed(BIT_REFUND_URL, body, timeout=_REST_TIMEOUT_S)
        except httpx.HTTPError as exc:
            raise ProviderTransient(f"bit refund request failed: {exc}") from exc
        return self._card_rest_result(data, amount)

    async def _refund_card(
        self, charge_id: str, amount: Money, ctx: RefundContext
    ) -> RefundResult:
        try:
            reference_txn_id = int(charge_id)
        except (TypeError, ValueError) as exc:
            raise ProviderPermanent(
                f"card refund reference_txn_id must be numeric, got {charge_id!r}"
            ) from exc
        # Always "credit" — Tranzila rejected the same-day "reversal" body (error 20004);
        # a credit note is money-safe pre- or post-settlement and legally identical.
        body = {
            "terminal_name": ctx.terminal_name or self._terminal_name,
            "txn_type": "credit",
            "reference_txn_id": reference_txn_id,
            "authorization_number": ctx.original_authnr or "0000000",
            "txn_currency_code": _ILS,
            "items": [
                {"name": "Refund", "unit_price": round(_minor_to_major(amount), 2),
                 "units_number": 1}
            ],
        }
        try:
            data = await self._post_signed(CARD_TXN_URL, body, timeout=_REST_TIMEOUT_S)
        except httpx.HTTPError as exc:
            raise ProviderTransient(f"card refund request failed: {exc}") from exc
        return self._card_rest_result(data, amount)

    @staticmethod
    def _card_rest_result(data: dict, amount: Money) -> RefundResult:
        """Map a card-REST/bit-refund envelope (``error_code``) to a RefundResult."""
        if data.get("error_code") != 0:
            raise ProviderPermanent(
                f"refund rejected: error_code={data.get('error_code')!r} "
                f"message={data.get('message')!r}"
            )
        result = data.get("transaction_result") or {}
        refund_id = str(result.get("index") or result.get("transaction_id") or "")
        return RefundResult(refund_id=refund_id, amount=amount)

    # --- notify handshake ------------------------------------------------------

    def parse_notify(self, headers: Mapping[str, str], body: bytes | str) -> NotifyEvent:
        """Parse Tranzila's FORM-encoded, UNSIGNED notify (SYNC, pure). ``verified`` is
        False — genuine authentication is the async Reports-API requery."""
        if isinstance(body, (bytes, bytearray)):
            body = bytes(body).decode("utf-8", "replace")
        raw = dict(parse_qsl(body, keep_blank_values=True))
        event_id = str(
            raw.get("index") or raw.get("TranzactionId") or raw.get("orderid") or ""
        )
        return NotifyEvent(provider=self.provider_name, event_id=event_id, verified=False, raw=raw)

    async def verify_notify(self, ev: NotifyEvent) -> NotifyEvent:
        """Authenticate a parsed notify via the HMAC-signed Reports API. A genuine
        mismatch returns a ``verified=False`` copy (not an exception); a Reports-API
        network blip raises :class:`ProviderTransient`."""
        raw = ev.raw or {}
        index = ev.event_id or str(raw.get("index") or "")
        try:
            transaction_index = int(index)
        except (TypeError, ValueError):
            return replace(ev, verified=False)

        sum_str = raw.get("sum") or raw.get("Amount") or raw.get("expected_sum") or "0"
        try:
            expected_agorot = round(float(sum_str) * 100)
        except (TypeError, ValueError):
            return replace(ev, verified=False)

        body = {"terminal_name": self._terminal_name, "transaction_index": transaction_index}
        try:
            data = await self._post_signed(REPORT_URL, body, timeout=_REPORT_TIMEOUT_S)
        except httpx.HTTPError as exc:
            raise ProviderTransient(f"Reports-API requery failed: {exc}") from exc

        txn = _extract_txn(data, transaction_index)
        if not txn:
            return replace(ev, verified=False)

        # `processor_response_code == "000"` is the authoritative approval gate;
        # `transtatus` is advisory only (a real Response=000 charge was seen with
        # transtatus=2), so it is NOT gated on.
        if str(txn.get("processor_response_code", "")) != "000":
            return replace(ev, verified=False)
        try:
            amount_agorot = int(txn["amount"])
        except (KeyError, TypeError, ValueError):
            return replace(ev, verified=False)
        if abs(amount_agorot - expected_agorot) > _SUM_TOLERANCE_AGOROT:
            return replace(ev, verified=False)
        return replace(ev, verified=True)

    async def finalize_from_notify(self, ev: NotifyEvent, idem_key: str) -> CaptureResult:
        """Turn a verified notify into a settled charge. The full paid-flip (re-price,
        invoice, SMS) stays above the seam."""
        if not ev.verified:
            raise ProviderPermanent("cannot finalize an unverified notify event")
        raw = ev.raw or {}
        sum_str = raw.get("sum") or raw.get("Amount") or raw.get("expected_sum") or "0"
        amount = Money(amount_minor=round(float(sum_str) * 100), currency=_ILS)
        return CaptureResult(charge_id=str(ev.event_id), amount=amount, channel="card")


__all__ = ["TranzilaPaymentProvider", "DEFAULT_PAYBRIDGE_URL"]
