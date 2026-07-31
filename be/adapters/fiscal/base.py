"""``FiscalProfile`` — the jurisdiction seam.

The CTO call (blueprint) is that the fiscal profile **owns document-number allocation
and document formatting**, not just tax constants. This keeps "who owns the counter"
in one place. In production, Postgres is the counter system-of-record behind
``allocate_number`` (an async, session-bound call), so numbering and rendering are
async. Some jurisdictions (Israel/Tranzila) mint the number only as a side effect of
creating the document, and the number is a vendor-owned **string** — so
``allocate_number`` returns ``str | int`` and ``issue_document`` exists for the
inseparable mint+render case.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from be.adapters.types import DocumentArtifact


class FiscalProfile(ABC):
    """Vendor/jurisdiction-neutral fiscal contract."""

    @abstractmethod
    async def allocate_number(self, kind: str, venue_id: str) -> str | int:
        """Allocate the next monotonic document number for ``(kind, venue_id)``.

        Returns ``int`` where a local counter owns the sequence (e.g. Postgres
        Z-numbers) or ``str`` where the vendor owns it. May raise
        ``NotImplementedError`` for kinds whose number is inseparable from creating
        the document (use :meth:`issue_document` for those).
        """

    @abstractmethod
    async def render_document(self, kind: str, order: dict) -> str:
        """Render the fiscal document of ``kind`` for ``order`` as a string artifact.

        ``kind`` selects the document (e.g. ``"receipt"``, ``"invoice"``, or a
        single combined ``"IR"`` in Israel). The return is the rendered text or a
        remote locator (URL / retrieval key) when the vendor stores the PDF.
        """

    async def issue_document(self, kind: str, order: dict) -> DocumentArtifact:
        """Mint a number and render a document as one operation.

        Default: allocate a number, inject it as ``order["document_number"]``, then
        render. Vendors where numbering and rendering are inseparable (Tranzila mints
        the invoice number as a side effect of ``create_invoice``) override this.
        """
        venue_id = str(order.get("venue_id", "")) if isinstance(order, dict) else ""
        number = await self.allocate_number(kind, venue_id)
        enriched = {**order, "document_number": number}
        content = await self.render_document(kind, enriched)
        return DocumentArtifact(kind=kind, number=number, content=content)

    @abstractmethod
    def vat_rate(self) -> float:
        """Return the applicable VAT rate as a fraction (e.g. ``0.18``)."""

    @abstractmethod
    def retention_years(self) -> int:
        """Return the legally required document-retention period in years."""


__all__ = ["FiscalProfile"]
