"""``Storage`` — the object-store seam.

``put``/``get`` move bytes by key; ``signed_url`` returns a time-limited URL. Note
the production GCS reality (see repo memory): the Cloud Run SA has no private key so
V4 signing fails — real impls proxy bytes through ``get`` and only expose
``signed_url`` where the credential actually supports it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Storage(ABC):
    """Vendor-neutral object-storage contract."""

    @abstractmethod
    async def put(self, key: str, data: bytes) -> str:
        """Store ``data`` under ``key``; return the stored key/URI."""

    @abstractmethod
    async def get(self, key: str) -> bytes:
        """Return the bytes stored under ``key``."""

    @abstractmethod
    def signed_url(self, key: str, ttl: int) -> str:
        """Return a URL valid for ``ttl`` seconds granting access to ``key``."""


__all__ = ["Storage"]
