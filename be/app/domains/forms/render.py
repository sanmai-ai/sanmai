"""Form-response PDF render seam — DEFERRED heavy backend.

Signing a form response should produce a deterministic legal PDF artifact (the live
system renders it with WeasyPrint and stores a ``pdf_sha256``). WeasyPrint is a heavy
system dependency (Cairo/Pango/GDK) that must NOT be pulled into the importable core,
so rendering lives behind this small seam — exactly the Null-projection posture used
for the menu read-copy.

* :class:`PdfRenderer` is the contract: given the template snapshot, the submitted
  response, and the employee, return ``(pdf_bytes, sha256_hex)`` — or ``None`` to defer.
* :class:`NullPdfRenderer` is the default: it renders nothing. The sign flow still
  records the signature + answers and completes the linked assignment item; the
  response's ``pdf_gcs_path`` / ``pdf_sha256`` simply stay ``NULL`` and the PDF-stream
  endpoints 404 until a real renderer is bound.

A later increment binds a WeasyPrint-backed renderer here (its own optional extra /
service) with **no domain-logic change** — only a different :class:`PdfRenderer`
injected at :func:`get_pdf_renderer`. Tests inject a fake renderer returning
deterministic bytes + hash to exercise the upload/stream path.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PdfRenderer(Protocol):
    """Render a submitted form response to ``(pdf_bytes, sha256_hex)`` — or ``None``."""

    def render(
        self, template: dict, response: dict, employee: dict
    ) -> tuple[bytes, str] | None:
        """Return the rendered PDF bytes + their sha256 hex, or ``None`` to defer."""
        ...


class NullPdfRenderer:
    """No-op renderer: defers the artifact (no bytes, no hash)."""

    def render(
        self, template: dict, response: dict, employee: dict
    ) -> tuple[bytes, str] | None:
        return None


def get_pdf_renderer() -> PdfRenderer:
    """FastAPI dependency: the bound :class:`PdfRenderer` (Null until wired)."""
    return NullPdfRenderer()


__all__ = ["PdfRenderer", "NullPdfRenderer", "get_pdf_renderer"]
