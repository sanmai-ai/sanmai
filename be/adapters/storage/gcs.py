"""``GcsStorage`` — real object-store adapter over ``google-cloud-storage``.

Self-contained: talks to the GCS SDK directly (no ``app.services.*``). Single bucket per
instance. Implements the revised :class:`Storage` ABC plus the optional
:class:`SupportsWriteOnce` capability (atomic ``if_generation_match=0`` create used by the
legal Z-report archive).

Round-trip safety: ``put`` / ``put_if_absent`` return the BARE key (not an ``https`` /
``gs://`` URI) so ``get(put(...))`` works; ``get`` also tolerates a ``gs://bucket/key``
prefix. The sync SDK calls run off the event loop via ``asyncio.to_thread``.

``signed_url`` is known-broken on Cloud Run (the keyless SA has no private key to sign V4
URLs) — a signing failure maps to :class:`ProviderConfigError` (misconfig), per the ABC.

Importing this module needs no creds: the SDK import is cred-free; ``storage.Client`` is
built in ``__init__`` (fail-loud at wiring), never at import.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta

from google.api_core import exceptions as gexc
from google.cloud import storage

from be.adapters.errors import ProviderConfigError, ProviderPermanent, ProviderTransient
from be.adapters.storage.base import Storage, SupportsWriteOnce

_DEFAULT_CONTENT_TYPE = "application/octet-stream"


def _strip_gs(key: str) -> str:
    """Accept a bare key OR a ``gs://bucket/key`` URI; return the object path."""
    if key.startswith("gs://"):
        return key[len("gs://") :].partition("/")[2]
    return key


class GcsStorage(Storage, SupportsWriteOnce):
    """Real GCS adapter — single bucket, write-once capable."""

    def __init__(
        self,
        *,
        bucket: str,
        project: str | None = None,
        public_base_url: str | None = None,
        client: storage.Client | None = None,
    ) -> None:
        if not bucket:
            raise ProviderConfigError("gcs bucket is required")
        self._public_base_url = public_base_url
        try:
            # Accept an injected client purely so tests pass a mock (never hits network).
            self._client = client if client is not None else storage.Client(project=project)
            self._bucket = self._client.bucket(bucket)
        except Exception as exc:  # noqa: BLE001 — bad creds/project -> fail loud at wiring
            raise ProviderConfigError(f"gcs client init failed: {exc}") from exc

    async def put(
        self, key: str, data: bytes, content_type: str = _DEFAULT_CONTENT_TYPE
    ) -> str:
        def _upload() -> None:
            blob = self._bucket.blob(_strip_gs(key))
            blob.upload_from_string(data, content_type=content_type)

        try:
            await asyncio.to_thread(_upload)
        except Exception as exc:  # noqa: BLE001 — upload is retryable
            raise ProviderTransient(f"gcs upload failed for {key!r}: {exc}") from exc
        return key

    async def get(self, key: str) -> bytes:
        def _download() -> bytes:
            blob = self._bucket.blob(_strip_gs(key))
            return blob.download_as_bytes()

        try:
            return await asyncio.to_thread(_download)
        except gexc.NotFound as exc:  # object genuinely absent — retry won't help
            raise ProviderPermanent(f"no object at key: {key}") from exc
        except Exception as exc:  # noqa: BLE001 — read failures are retryable
            raise ProviderTransient(f"gcs download failed for {key!r}: {exc}") from exc

    def signed_url(self, key: str, ttl: int) -> str:
        try:
            blob = self._bucket.blob(_strip_gs(key))
            return blob.generate_signed_url(
                version="v4",
                expiration=timedelta(seconds=ttl),
                method="GET",
            )
        except Exception as exc:  # noqa: BLE001 — keyless SA cannot sign on Cloud Run
            raise ProviderConfigError(
                f"V4 signing unavailable (SA has no private key?): {exc}"
            ) from exc

    async def put_if_absent(
        self, key: str, data: bytes, content_type: str = _DEFAULT_CONTENT_TYPE
    ) -> tuple[str, str]:
        object_key = _strip_gs(key)
        new_hash = hashlib.sha256(data).hexdigest()

        def _write_once() -> str:
            blob = self._bucket.blob(object_key)
            if blob.exists():  # write-once: never overwrite; hash the STORED bytes
                return hashlib.sha256(blob.download_as_bytes()).hexdigest()
            try:
                # if_generation_match=0 makes the create atomic — fails if a concurrent
                # writer created the object between exists() and now.
                blob.upload_from_string(
                    data, content_type=content_type, if_generation_match=0
                )
            except gexc.PreconditionFailed:  # lost the race — read the winner's bytes
                return hashlib.sha256(blob.download_as_bytes()).hexdigest()
            return new_hash

        try:
            digest = await asyncio.to_thread(_write_once)
        except Exception as exc:  # noqa: BLE001 — upload/read is retryable
            raise ProviderTransient(f"gcs write-once failed for {key!r}: {exc}") from exc
        return key, digest


__all__ = ["GcsStorage"]
