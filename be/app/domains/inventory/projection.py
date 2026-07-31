"""Inventory read-copy projection seam — DEFERRED implementation.

In the live system some inventory writes also mirror into a fast document-store read
copy — purchase-order documents (an ops queue) and stock-transfer records. The core
has **no document-store adapter yet**, so this ships as a no-op seam, mirroring
:class:`be.app.domains.menu.projection.MenuProjection`.

The Postgres source-of-truth (items, stock, suppliers, purchasing, aliases) is fully
functional without it. A later increment binds a real document-store-backed
projection here — no domain-logic change required, only a different
:class:`InventoryProjection` injected at the seam.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class InventoryProjection(Protocol):
    """Write seam to the fast inventory read-copy (deferred backend)."""

    def publish_purchase_order(self, order_id: str, venue_id: str, order: dict) -> None:
        """Mirror a placed/received purchase order to the read copy (no-op until wired)."""
        ...

    def publish_stock(self, inventory_sku: str, venue_id: str, by_location: dict) -> None:
        """Mirror a stock change to the read copy (no-op until wired)."""
        ...


class NullInventoryProjection:
    """No-op projection: Postgres is the sole store until a doc-store adapter lands."""

    def publish_purchase_order(self, order_id: str, venue_id: str, order: dict) -> None:
        return None

    def publish_stock(self, inventory_sku: str, venue_id: str, by_location: dict) -> None:
        return None


__all__ = ["InventoryProjection", "NullInventoryProjection"]
