"""``GenericFiscalProfile`` — zero-jurisdiction fake.

In-memory monotonic counters keyed by ``(kind, venue_id)``, 0.0 VAT, and plain-text
document rendering. No DB, no credentials. Suitable for the OSS default and tests.
"""

from __future__ import annotations

from be.adapters.fiscal.base import FiscalProfile


class GenericFiscalProfile(FiscalProfile):
    """A minimal, dependency-free fiscal profile."""

    def __init__(self, retention_years: int = 7) -> None:
        self._counters: dict[tuple[str, str], int] = {}
        self._retention_years = retention_years

    def allocate_number(self, kind: str, venue_id: str) -> int:
        key = (kind, venue_id)
        nxt = self._counters.get(key, 0) + 1
        self._counters[key] = nxt
        return nxt

    def render_receipt(self, order: dict) -> str:
        return self._render("RECEIPT", order)

    def render_invoice(self, order: dict) -> str:
        return self._render("TAX INVOICE", order)

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
