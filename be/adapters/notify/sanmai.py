"""``SanMaiNotifier`` — composite router over the per-channel notifiers.

Routes on the ``kind`` prefix to a sub-notifier (Telegram / Gmail / SMS), each of which
independently conforms to :class:`Notifier`. A single-channel deploy can wire just one
sub-notifier; unrouted kinds and unconfigured channels return ``SendResult(ok=False, ...)``.

Best-effort per the contract: ``send`` never raises (each sub-notifier already guarantees
this, and the router wraps dispatch defensively).

Routing:
  ``telegram_*``            -> Telegram
  ``gmail_*`` / ``gmail``   -> Gmail
  ``sms_*`` / ``sms``       -> SMS
"""

from __future__ import annotations

from be.adapters.notify.base import Notifier
from be.adapters.types import SendResult


class SanMaiNotifier(Notifier):
    """Composite notifier — dispatches by ``kind`` prefix to a channel notifier."""

    def __init__(
        self,
        *,
        telegram: Notifier | None = None,
        gmail: Notifier | None = None,
        sms: Notifier | None = None,
    ) -> None:
        self._telegram = telegram
        self._gmail = gmail
        self._sms = sms

    async def send(self, kind: str, payload: dict) -> SendResult:
        try:
            channel, target = self._route(kind)
        except Exception as exc:  # noqa: BLE001 — Notifier.send must NEVER raise
            return SendResult(ok=False, detail=str(exc))
        if channel is None:
            return SendResult(ok=False, detail=f"unknown notify kind: {kind!r}")
        if target is None:
            return SendResult(ok=False, detail=f"{channel} notifier not configured")
        return await target.send(kind, payload)

    def _route(self, kind: str) -> tuple[str | None, Notifier | None]:
        if kind.startswith("telegram"):
            return "telegram", self._telegram
        if kind == "gmail" or kind.startswith("gmail_"):
            return "gmail", self._gmail
        if kind == "sms" or kind.startswith("sms_"):
            return "sms", self._sms
        return None, None


__all__ = ["SanMaiNotifier"]
