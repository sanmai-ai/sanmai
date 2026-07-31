"""``NoopNotifier`` — swallows notifications and reports success.

Records every send in ``sent`` so tests can assert routing without any external
service. Always returns ``SendResult(ok=True)``.
"""

from __future__ import annotations

from be.adapters.notify.base import Notifier
from be.adapters.types import SendResult


class NoopNotifier(Notifier):
    """A do-nothing notifier that keeps an in-memory outbox."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send(self, kind: str, payload: dict) -> SendResult:
        self.sent.append((kind, payload))
        return SendResult(ok=True, detail=f"noop:{kind}")


__all__ = ["NoopNotifier"]
