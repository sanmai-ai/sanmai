"""``FiscalProfile`` — the jurisdiction seam.

The CTO call (blueprint) is that the fiscal profile **owns document-number allocation
and document formatting**, not just tax constants. This keeps "who owns the counter"
in one place. In production, Postgres is the counter system-of-record behind
``allocate_number``; the generic fake keeps an in-memory monotonic counter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class FiscalProfile(ABC):
    """Vendor/jurisdiction-neutral fiscal contract."""

    @abstractmethod
    def allocate_number(self, kind: str, venue_id: str) -> int:
        """Allocate the next monotonic document number for ``(kind, venue_id)``."""

    @abstractmethod
    def render_receipt(self, order: dict) -> str:
        """Render a customer receipt for ``order`` as a string artifact."""

    @abstractmethod
    def render_invoice(self, order: dict) -> str:
        """Render a tax invoice for ``order`` as a string artifact."""

    @abstractmethod
    def vat_rate(self) -> float:
        """Return the applicable VAT rate as a fraction (e.g. ``0.17``)."""

    @abstractmethod
    def retention_years(self) -> int:
        """Return the legally required document-retention period in years."""


__all__ = ["FiscalProfile"]
