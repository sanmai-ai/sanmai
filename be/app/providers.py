"""Provider factories — map config provider *ids* to concrete adapter instances.

:class:`be.config.Settings` stores which provider each port binds to as a string
(``llm_provider="echo"``, ``storage_provider="local"``, ``identity_provider="static"``).
These pure factories resolve that id to a concrete implementation, raising
:class:`ProviderConfigError` on an unknown id (fail-loud at wiring time, never deep
in a request).

The zero-credential OSS fakes (echo/local/static) always ship in the core. The real
vendor adapters (``gemini``/``gcs``/``firebase``) also live in core but import their
vendor SDK lazily *inside* the selected branch — so importing this module needs no
vendor SDKs and no credentials; a real adapter's creds are resolved (and validated
fail-loud) only when that provider is selected and constructed.
"""

from __future__ import annotations

from be.adapters.errors import ProviderConfigError
from be.adapters.identity.base import IdentityProvider
from be.adapters.identity.static import StaticIdentityProvider
from be.adapters.llm.base import LLMProvider
from be.adapters.llm.echo import EchoLLMProvider
from be.adapters.storage.base import Storage
from be.adapters.storage.local import LocalStorage
from be.config import Settings


def build_llm(settings: Settings) -> LLMProvider:
    """Resolve :data:`settings.llm_provider` to a concrete :class:`LLMProvider`."""
    provider_id = settings.llm_provider
    if provider_id == "echo":
        return EchoLLMProvider()
    if provider_id == "gemini":
        if not settings.gemini_api_key:
            raise ProviderConfigError(
                "llm provider 'gemini' requires SANMAI_GEMINI_API_KEY"
            )
        # Vendor import kept inside the branch so importing providers.py stays SDK-free.
        from be.adapters.llm.gemini import GeminiLLMProvider

        return GeminiLLMProvider(
            api_key=settings.gemini_api_key, model=settings.gemini_model
        )
    raise ProviderConfigError(f"unknown llm provider id: {provider_id!r}")


def build_storage(settings: Settings) -> Storage:
    """Resolve :data:`settings.storage_provider` to a concrete :class:`Storage`."""
    provider_id = settings.storage_provider
    if provider_id == "local":
        return LocalStorage()
    if provider_id == "gcs":
        if not settings.gcs_bucket:
            raise ProviderConfigError(
                "storage provider 'gcs' requires SANMAI_GCS_BUCKET"
            )
        from be.adapters.storage.gcs import GcsStorage

        return GcsStorage(
            bucket=settings.gcs_bucket,
            project=settings.gcs_project,
            public_base_url=settings.gcs_public_base_url,
        )
    raise ProviderConfigError(f"unknown storage provider id: {provider_id!r}")


def build_identity(settings: Settings) -> IdentityProvider:
    """Resolve :data:`settings.identity_provider` to an :class:`IdentityProvider`."""
    provider_id = settings.identity_provider
    if provider_id == "static":
        return StaticIdentityProvider()
    if provider_id == "firebase":
        from be.adapters.identity.firebase import FirebaseIdentityProvider

        return FirebaseIdentityProvider(
            credentials_json=settings.firebase_credentials_json,
            credentials_path=settings.firebase_credentials_path,
            project_id=settings.firebase_project_id,
        )
    raise ProviderConfigError(f"unknown identity provider id: {provider_id!r}")


__all__ = ["build_llm", "build_storage", "build_identity"]
