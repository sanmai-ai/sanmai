"""Unit tests for the provider factory — config id -> concrete adapter (fail-loud)."""

from __future__ import annotations

import pytest

from be.adapters.errors import ProviderConfigError
from be.adapters.identity.static import StaticIdentityProvider
from be.adapters.llm.echo import EchoLLMProvider
from be.adapters.storage.local import LocalStorage
from be.app.providers import build_identity, build_llm, build_storage
from be.config import get_settings


def _settings(**overrides: str):  # type: ignore[no-untyped-def]
    return get_settings().model_copy(update=overrides)


def test_build_llm_resolves_and_rejects() -> None:
    assert isinstance(build_llm(_settings(llm_provider="echo")), EchoLLMProvider)
    with pytest.raises(ProviderConfigError, match="not bundled"):
        build_llm(_settings(llm_provider="gemini"))
    with pytest.raises(ProviderConfigError, match="unknown"):
        build_llm(_settings(llm_provider="nope"))


def test_build_storage_resolves_and_rejects() -> None:
    assert isinstance(build_storage(_settings(storage_provider="local")), LocalStorage)
    with pytest.raises(ProviderConfigError, match="not bundled"):
        build_storage(_settings(storage_provider="gcs"))
    with pytest.raises(ProviderConfigError, match="unknown"):
        build_storage(_settings(storage_provider="nope"))


def test_build_identity_resolves_and_rejects() -> None:
    assert isinstance(build_identity(_settings(identity_provider="static")), StaticIdentityProvider)
    with pytest.raises(ProviderConfigError, match="not bundled"):
        build_identity(_settings(identity_provider="firebase"))
    with pytest.raises(ProviderConfigError, match="unknown"):
        build_identity(_settings(identity_provider="nope"))
