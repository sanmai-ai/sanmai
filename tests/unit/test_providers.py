"""Unit tests for the provider factory — config id -> concrete adapter (fail-loud).

Real vendor adapters (gemini/gcs/firebase) now build in-core, but their SDKs import
lazily and creds resolve at construction — so these tests never touch a network or a
real credential. Missing-cred selection must fail loud with ``ProviderConfigError``.
"""

from __future__ import annotations

from unittest import mock

import pytest

from be.adapters.errors import ProviderConfigError
from be.adapters.identity.static import StaticIdentityProvider
from be.adapters.llm.echo import EchoLLMProvider
from be.adapters.storage.local import LocalStorage
from be.app.providers import build_identity, build_llm, build_storage
from be.config import get_settings


def _settings(**overrides):  # type: ignore[no-untyped-def]
    return get_settings().model_copy(update=overrides)


def test_build_llm_resolves_and_rejects() -> None:
    assert isinstance(build_llm(_settings(llm_provider="echo")), EchoLLMProvider)
    with pytest.raises(ProviderConfigError, match="unknown"):
        build_llm(_settings(llm_provider="nope"))


def test_build_llm_gemini_requires_key_then_builds() -> None:
    with pytest.raises(ProviderConfigError, match="SANMAI_GEMINI_API_KEY"):
        build_llm(_settings(llm_provider="gemini", gemini_api_key=None))

    with mock.patch("google.generativeai.configure") as configure:
        from be.adapters.llm.gemini import GeminiLLMProvider

        prov = build_llm(_settings(llm_provider="gemini", gemini_api_key="k"))
        assert isinstance(prov, GeminiLLMProvider)
        configure.assert_called_once_with(api_key="k")


def test_build_storage_resolves_and_rejects() -> None:
    assert isinstance(build_storage(_settings(storage_provider="local")), LocalStorage)
    with pytest.raises(ProviderConfigError, match="unknown"):
        build_storage(_settings(storage_provider="nope"))


def test_build_storage_gcs_requires_bucket_then_builds() -> None:
    with pytest.raises(ProviderConfigError, match="SANMAI_GCS_BUCKET"):
        build_storage(_settings(storage_provider="gcs", gcs_bucket=None))

    with mock.patch("google.cloud.storage.Client") as client_cls:
        from be.adapters.storage.gcs import GcsStorage

        prov = build_storage(_settings(storage_provider="gcs", gcs_bucket="b"))
        assert isinstance(prov, GcsStorage)
        client_cls.assert_called_once()


def test_build_identity_resolves_and_rejects() -> None:
    assert isinstance(build_identity(_settings(identity_provider="static")), StaticIdentityProvider)
    with pytest.raises(ProviderConfigError, match="unknown"):
        build_identity(_settings(identity_provider="nope"))


def test_build_identity_firebase_builds_with_adc() -> None:
    with (
        mock.patch("firebase_admin.credentials.ApplicationDefault") as adc,
        mock.patch("firebase_admin.initialize_app") as init_app,
    ):
        from be.adapters.identity.firebase import FirebaseIdentityProvider

        prov = build_identity(_settings(identity_provider="firebase"))
        assert isinstance(prov, FirebaseIdentityProvider)
        adc.assert_called_once()
        init_app.assert_called_once()
