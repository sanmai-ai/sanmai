"""``Storage`` — the object-store seam.

``put``/``get`` move bytes by key; ``signed_url`` returns a time-limited URL. Note
the production GCS reality (see repo memory): the Cloud Run SA has no private key so
V4 signing fails — real impls proxy bytes through ``get`` and only expose
``signed_url`` where the credential actually supports it.

``content_type`` is REQUIRED-with-default on ``put``: the real GCS helpers
(``upload_to_gcs``/``upload_proof``) demand a MIME type and the flat ``put`` was lossy
for arbitrary uploads (Increment-1 gap: "the sharpest gap").

Write-once is an OPTIONAL capability (:class:`SupportsWriteOnce`) — the legal Z-report
archive needs atomic create-or-return-existing plus the content hash, which a plain
``put`` cannot express. Not every backend can do ``if_generation_match=0``, so it is a
mixin, not a base method.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

_DEFAULT_CONTENT_TYPE = "application/octet-stream"


class Storage(ABC):
    """Vendor-neutral object-storage contract."""

    @abstractmethod
    async def put(
        self, key: str, data: bytes, content_type: str = _DEFAULT_CONTENT_TYPE
    ) -> str:
        """Store ``data`` under ``key`` with ``content_type``; return the stored key/URI."""

    @abstractmethod
    async def get(self, key: str) -> bytes:
        """Return the bytes stored under ``key``."""

    @abstractmethod
    def signed_url(self, key: str, ttl: int) -> str:
        """Return a URL valid for ``ttl`` seconds granting access to ``key``."""


class SupportsWriteOnce(ABC):
    """Optional capability: atomic write-once storage for immutable legal artifacts.

    Mirrors ``app.services.gcs_archive.archive_artifacts`` — a minted Z-report's
    PDF/CSV/XLSX is the legal record and must never be silently overwritten.
    Implementations create the object only if absent (GCS ``if_generation_match=0``);
    if it already exists they return the EXISTING url and the sha256 of the stored
    bytes (idempotent, drift-tolerant).
    """

    @abstractmethod
    async def put_if_absent(
        self, key: str, data: bytes, content_type: str = _DEFAULT_CONTENT_TYPE
    ) -> tuple[str, str]:
        """Create ``key`` iff absent; return ``(url, sha256_hex)``.

        If ``key`` already exists, do NOT overwrite — return the existing object's url
        and the sha256 of the stored bytes.
        """


__all__ = ["Storage", "SupportsWriteOnce"]
