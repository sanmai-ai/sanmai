"""``LocalStorage`` — tempdir-backed fake object store.

Writes blobs to a local directory (a fresh ``tempfile.mkdtemp()`` by default). Keys
are sanitised to a flat filename so nested keys are safe. ``signed_url`` returns a
``file://`` URI (the ``ttl`` is accepted but not enforced — there is no signing
infra locally). Zero credentials.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from be.adapters.errors import ProviderPermanent
from be.adapters.storage.base import Storage


class LocalStorage(Storage):
    """Filesystem-backed storage fake."""

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root) if root else Path(tempfile.mkdtemp(prefix="sanmai_store_"))
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = key.strip("/").replace("/", "__")
        if not safe:
            raise ProviderPermanent("storage key must be non-empty")
        return self.root / safe

    async def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.write_bytes(data)
        return key

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise ProviderPermanent(f"no object at key: {key}")
        return path.read_bytes()

    def signed_url(self, key: str, ttl: int) -> str:
        return self._path(key).as_uri()


__all__ = ["LocalStorage"]
