"""``SmsNotifier`` — real SMS-gateway notifier over ``httpx``.

Self-contained: POSTs the configured gateway directly (no ``app.services.*``). The
env/TEST_MODE/QA-whitelist dry-run overlay from the live service is intentionally dropped
— that is a deployment concern, not the adapter's. Here ``ok=False`` means a genuine send
failure only.

Best-effort per the :class:`Notifier` contract: any failure returns
``SendResult(ok=False, ...)`` — ``send`` never raises.
"""

from __future__ import annotations

import httpx

from be.adapters.notify.base import Notifier
from be.adapters.types import SendResult

_TIMEOUT = 10.0


class SmsNotifier(Notifier):
    """Real SMS-gateway notifier — handles ``sms_*`` kinds."""

    def __init__(
        self,
        *,
        gateway_url: str,
        api_key: str,
        sender_name: str = "SanMai",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._gateway_url = gateway_url
        self._api_key = api_key
        self._sender_name = sender_name
        self._client = http_client

    async def send(self, kind: str, payload: dict) -> SendResult:
        try:
            return await self._send_sms(payload or {})
        except Exception as exc:  # noqa: BLE001 — Notifier.send must NEVER raise
            return SendResult(ok=False, detail=str(exc))

    async def _send_sms(self, p: dict) -> SendResult:
        if not (self._gateway_url and self._api_key):
            return SendResult(ok=False, detail="sms gateway not configured")
        phone = p.get("phone")
        message = p.get("message")
        if not phone or not message:
            return SendResult(ok=False, detail="sms payload requires 'phone' and 'message'")
        body = {
            "to": phone,
            "message": message,
            "from": self._sender_name,
            "api_key": self._api_key,
        }
        if self._client is not None:
            resp = await self._client.post(self._gateway_url, json=body, timeout=_TIMEOUT)
        else:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(self._gateway_url, json=body)
        resp.raise_for_status()
        return SendResult(ok=True, detail=f"sms sent to {phone}")


__all__ = ["SmsNotifier"]
