"""``Notifier`` — best-effort outbound-notification seam.

``send`` routes per ``kind`` (e.g. ``"telegram_approval"``, ``"gmail_accountant"``,
``"sms_status"``). Semantics are **best-effort**: a failure returns
``SendResult(ok=False, ...)`` rather than raising, so notification problems never
break the business transaction that triggered them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from be.adapters.types import SendResult


class Notifier(ABC):
    """Vendor-neutral, best-effort notification contract."""

    @abstractmethod
    async def send(self, kind: str, payload: dict) -> SendResult:
        """Send a ``kind`` notification carrying ``payload``. Never raises."""


__all__ = ["Notifier"]
