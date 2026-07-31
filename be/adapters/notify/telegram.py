"""``TelegramNotifier`` — real Telegram Bot API notifier over ``httpx``.

Self-contained: POSTs ``sendMessage`` to the Bot API directly (no ``app.services.*``,
no Postgres). The adapter does ONLY the vendor call — the domain layer pre-renders the
card ``text`` / ``reply_markup`` and does its own approval-message tracking, then hands a
ready payload down.

Best-effort per the :class:`Notifier` contract: any failure (missing token, HTTP error,
``ok: false``, network blip) returns ``SendResult(ok=False, ...)`` — ``send`` never raises.

Importing this module needs no token: the token is validated (and the client built) at
construction, never at import.
"""

from __future__ import annotations

import httpx

from be.adapters.notify.base import Notifier
from be.adapters.types import SendResult

_API_BASE = "https://api.telegram.org"
_TIMEOUT = 10.0


class TelegramNotifier(Notifier):
    """Real Telegram Bot API notifier — handles ``telegram_*`` kinds."""

    def __init__(
        self,
        *,
        bot_token: str,
        default_chat_id: int | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._default_chat_id = default_chat_id
        self._client = http_client

    async def send(self, kind: str, payload: dict) -> SendResult:
        try:
            return await self._send_message(payload or {})
        except Exception as exc:  # noqa: BLE001 — Notifier.send must NEVER raise
            return SendResult(ok=False, detail=str(exc))

    async def _send_message(self, p: dict) -> SendResult:
        if not self._bot_token:
            return SendResult(ok=False, detail="telegram bot token not configured")
        text = p.get("text")
        if not text:
            return SendResult(ok=False, detail="telegram payload requires 'text'")
        chat_id = p.get("chat_id", self._default_chat_id)
        if chat_id is None:
            return SendResult(ok=False, detail="telegram payload requires a chat_id")

        body: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if p.get("reply_markup") is not None:
            body["reply_markup"] = p["reply_markup"]
        if p.get("reply_to_message_id") is not None:
            body["reply_to_message_id"] = p["reply_to_message_id"]

        url = f"{_API_BASE}/bot{self._bot_token}/sendMessage"
        data = await self._post(url, body)
        if not isinstance(data, dict) or not data.get("ok"):
            return SendResult(ok=False, detail=f"telegram sendMessage failed: {data!r}")
        message_id = (data.get("result") or {}).get("message_id")
        return SendResult(ok=message_id is not None, detail=str(message_id or ""))

    async def _post(self, url: str, body: dict) -> object:
        if self._client is not None:
            resp = await self._client.post(url, json=body, timeout=_TIMEOUT)
            return resp.json()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=body)
            return resp.json()


__all__ = ["TelegramNotifier"]
