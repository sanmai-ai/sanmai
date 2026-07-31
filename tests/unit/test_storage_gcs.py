"""Unit tests for the real GcsStorage — google-cloud-storage mocked, no network.

Assert put/get translation (content_type, bare-key round-trip, gs:// tolerance),
write-once semantics (if_generation_match=0, drift-tolerant hash), and the error
taxonomy (Transient upload/read, Permanent NotFound, ConfigError signing). Uses the
injected ``client=`` ctor param so the SDK never touches a network.
"""

from __future__ import annotations

import hashlib
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

from be.adapters.errors import ProviderConfigError, ProviderPermanent, ProviderTransient
from be.adapters.storage.base import Storage, SupportsWriteOnce
from be.adapters.storage.gcs import GcsStorage


def _storage() -> tuple[GcsStorage, mock.Mock, mock.Mock]:
    """Return (storage, bucket_mock, blob_mock) with an injected fake client."""
    blob = mock.Mock()
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    client = mock.Mock()
    client.bucket.return_value = bucket
    return GcsStorage(bucket="my-bucket", client=client), bucket, blob


def test_import_is_credential_free() -> None:
    import importlib

    import be.adapters.storage.gcs as mod

    importlib.reload(mod)  # importing must not build a client / need creds


def test_is_storage_and_write_once() -> None:
    s, _, _ = _storage()
    assert isinstance(s, Storage)
    assert isinstance(s, SupportsWriteOnce)


def test_empty_bucket_raises_config_error() -> None:
    with pytest.raises(ProviderConfigError):
        GcsStorage(bucket="", client=mock.Mock())


async def test_put_translation_and_bare_key_roundtrip() -> None:
    s, bucket, blob = _storage()
    key = await s.put("docs/report.pdf", b"%PDF", content_type="application/pdf")
    assert key == "docs/report.pdf"  # bare key, not an https/gs URI
    bucket.blob.assert_called_once_with("docs/report.pdf")
    blob.upload_from_string.assert_called_once_with(b"%PDF", content_type="application/pdf")


async def test_put_default_content_type() -> None:
    s, _, blob = _storage()
    await s.put("k", b"raw")
    _, kwargs = blob.upload_from_string.call_args
    assert kwargs["content_type"] == "application/octet-stream"


async def test_put_failure_is_transient() -> None:
    s, _, blob = _storage()
    blob.upload_from_string.side_effect = gexc.GoogleAPICallError("boom")
    with pytest.raises(ProviderTransient):
        await s.put("k", b"x")


async def test_get_strips_gs_prefix() -> None:
    s, bucket, blob = _storage()
    blob.download_as_bytes.return_value = b"bytes"
    out = await s.get("gs://my-bucket/docs/x.pdf")
    assert out == b"bytes"
    bucket.blob.assert_called_once_with("docs/x.pdf")


async def test_get_not_found_is_permanent() -> None:
    s, _, blob = _storage()
    blob.download_as_bytes.side_effect = gexc.NotFound("missing")
    with pytest.raises(ProviderPermanent):
        await s.get("nope")


async def test_get_other_failure_is_transient() -> None:
    s, _, blob = _storage()
    blob.download_as_bytes.side_effect = gexc.ServiceUnavailable("503")
    with pytest.raises(ProviderTransient):
        await s.get("k")


def test_signed_url_signing_failure_is_config_error() -> None:
    s, _, blob = _storage()
    blob.generate_signed_url.side_effect = Exception("no private key")
    with pytest.raises(ProviderConfigError):
        s.signed_url("k", ttl=60)


def test_signed_url_passes_ttl_seconds() -> None:
    from datetime import timedelta

    s, _, blob = _storage()
    blob.generate_signed_url.return_value = "https://signed"
    assert s.signed_url("k", ttl=120) == "https://signed"
    _, kwargs = blob.generate_signed_url.call_args
    assert kwargs["expiration"] == timedelta(seconds=120)
    assert kwargs["version"] == "v4"
    assert kwargs["method"] == "GET"


async def test_put_if_absent_creates_atomically() -> None:
    s, _, blob = _storage()
    blob.exists.return_value = False
    data = b"Z-report"
    key, digest = await s.put_if_absent("2026/Z-1.pdf", data, "application/pdf")
    assert key == "2026/Z-1.pdf"
    assert digest == hashlib.sha256(data).hexdigest()
    _, kwargs = blob.upload_from_string.call_args
    assert kwargs["if_generation_match"] == 0  # atomic write-once
    assert kwargs["content_type"] == "application/pdf"


async def test_put_if_absent_existing_returns_stored_hash() -> None:
    s, _, blob = _storage()
    stored = b"original-archived"
    blob.exists.return_value = True
    blob.download_as_bytes.return_value = stored
    key, digest = await s.put_if_absent("2026/Z-1.pdf", b"tampered", "application/pdf")
    assert key == "2026/Z-1.pdf"
    assert digest == hashlib.sha256(stored).hexdigest()  # stored bytes, not tampered
    blob.upload_from_string.assert_not_called()  # write-once: no overwrite


async def test_put_if_absent_race_resolves_idempotently() -> None:
    s, _, blob = _storage()
    winner = b"winner-bytes"
    blob.exists.return_value = False  # looked absent...
    blob.upload_from_string.side_effect = gexc.PreconditionFailed("lost the race")
    blob.download_as_bytes.return_value = winner
    key, digest = await s.put_if_absent("k", b"mine", "text/plain")
    assert digest == hashlib.sha256(winner).hexdigest()


async def test_put_if_absent_failure_is_transient() -> None:
    s, _, blob = _storage()
    blob.exists.side_effect = gexc.ServiceUnavailable("503")
    with pytest.raises(ProviderTransient):
        await s.put_if_absent("k", b"x")
