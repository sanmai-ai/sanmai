"""``GenericFiscalProfile`` — zero-jurisdiction fake.

In-memory monotonic counters keyed by ``(kind, venue_id)``, 0.0 VAT, and plain-text
document rendering. No DB, no credentials. Async to match the real (session-bound)
counter; an ``asyncio.Lock`` keeps the counter monotonic under concurrent awaits.
Suitable for the OSS default and tests.
"""

from __future__ import annotations

import asyncio

from be.adapters.fiscal.base import FiscalProfile

_TITLES = {"receipt": "RECEIPT", "invoice": "TAX INVOICE"}


class GenericFiscalProfile(FiscalProfile):
    """A minimal, dependency-free fiscal profile."""

    def __init__(self, retention_years: int = 7) -> None:
        self._counters: dict[tuple[str, str], int] = {}
        self._retention_years = retention_years
        self._lock = asyncio.Lock()

    async def allocate_number(self, kind: str, venue_id: str) -> str | int:
        key = (kind, venue_id)
        async with self._lock:
            nxt = self._counters.get(key, 0) + 1
            self._counters[key] = nxt
            return nxt

    async def render_document(self, kind: str, order: dict) -> str:
        title = _TITLES.get(kind, kind.upper())
        return self._render(title, order)

    def vat_rate(self) -> float:
        return 0.0

    def retention_years(self) -> int:
        return self._retention_years

    @staticmethod
    def _render(title: str, order: dict) -> str:
        lines = [title]
        order_id = order.get("id") or order.get("order_id")
        if order_id is not None:
            lines.append(f"Order: {order_id}")
        number = order.get("document_number")
        if number is not None:
            lines.append(f"No: {number}")
        for item in order.get("items", []):
            name = item.get("name", "item")
            qty = item.get("qty", 1)
            price = item.get("price_minor", 0)
            lines.append(f"{qty} x {name} @ {price}")
        total = order.get("total_minor")
        if total is not None:
            lines.append(f"TOTAL: {total}")
        return "\n".join(lines)


__all__ = ["GenericFiscalProfile"]
