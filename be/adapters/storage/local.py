"""``LocalStorage`` — tempdir-backed fake object store.

Writes blobs to a local directory (a fresh ``tempfile.mkdtemp()`` by default). Keys
are sanitised to a flat filename so nested keys are safe. ``signed_url`` returns a
``file://`` URI (the ``ttl`` is accepted but not enforced — there is no signing infra
locally). ``put_if_absent`` emulates GCS write-once via ``Path.exists()`` + sha256.
Zero credentials.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from be.adapters.errors import ProviderPermanent
from be.adapters.storage.base import Storage, SupportsWriteOnce

_DEFAULT_CONTENT_TYPE = "application/octet-stream"


class LocalStorage(Storage, SupportsWriteOnce):
    """Filesystem-backed storage fake — implements the write-once capability too."""

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root) if root else Path(tempfile.mkdtemp(prefix="sanmai_store_"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.content_types: dict[str, str] = {}

    def _path(self, key: str) -> Path:
        safe = key.strip("/").replace("/", "__")
        if not safe:
            raise ProviderPermanent("storage key must be non-empty")
        return self.root / safe

    async def put(
        self, key: str, data: bytes, content_type: str = _DEFAULT_CONTENT_TYPE
    ) -> str:
        path = self._path(key)
        path.write_bytes(data)
        self.content_types[key] = content_type
        return key

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise ProviderPermanent(f"no object at key: {key}")
        return path.read_bytes()

    def signed_url(self, key: str, ttl: int) -> str:
        return self._path(key).as_uri()

    async def put_if_absent(
        self, key: str, data: bytes, content_type: str = _DEFAULT_CONTENT_TYPE
    ) -> tuple[str, str]:
        path = self._path(key)
        if path.exists():  # write-once: never overwrite; hash the STORED bytes
            existing = path.read_bytes()
            return key, hashlib.sha256(existing).hexdigest()
        path.write_bytes(data)
        self.content_types[key] = content_type
        return key, hashlib.sha256(data).hexdigest()


__all__ = ["LocalStorage"]
