"""Provider factories — map config provider *ids* to concrete adapter instances.

:class:`be.config.Settings` stores which provider each port binds to as a string
(``llm_provider="echo"``, ``storage_provider="local"``, ``identity_provider="static"``).
These pure factories resolve that id to a concrete implementation, raising
:class:`ProviderConfigError` on an unknown id (fail-loud at wiring time, never deep
in a request).

Only the zero-credential OSS fakes ship in the core. Real vendor adapters
(``gemini``/``gcs``/``firebase``) live in the private overlay and register by binding
a different id here or overriding the FastAPI dependency — so importing the core needs
no vendor SDKs and no credentials.
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

# Vendor ids the core recognises but does NOT bundle (overlay-only). Named so the
# error message tells the operator exactly what is missing rather than "unknown".
_OVERLAY_LLM = {"gemini"}
_OVERLAY_STORAGE = {"gcs"}
_OVERLAY_IDENTITY = {"firebase"}


def build_llm(settings: Settings) -> LLMProvider:
    """Resolve :data:`settings.llm_provider` to a concrete :class:`LLMProvider`."""
    provider_id = settings.llm_provider
    if provider_id == "echo":
        return EchoLLMProvider()
    if provider_id in _OVERLAY_LLM:
        raise ProviderConfigError(
            f"llm provider {provider_id!r} is not bundled in the core "
            "(install the overlay adapter or set SANMAI_LLM_PROVIDER=echo)"
        )
    raise ProviderConfigError(f"unknown llm provider id: {provider_id!r}")


def build_storage(settings: Settings) -> Storage:
    """Resolve :data:`settings.storage_provider` to a concrete :class:`Storage`."""
    provider_id = settings.storage_provider
    if provider_id == "local":
        return LocalStorage()
    if provider_id in _OVERLAY_STORAGE:
        raise ProviderConfigError(
            f"storage provider {provider_id!r} is not bundled in the core "
            "(install the overlay adapter or set SANMAI_STORAGE_PROVIDER=local)"
        )
    raise ProviderConfigError(f"unknown storage provider id: {provider_id!r}")


def build_identity(settings: Settings) -> IdentityProvider:
    """Resolve :data:`settings.identity_provider` to an :class:`IdentityProvider`."""
    provider_id = settings.identity_provider
    if provider_id == "static":
        return StaticIdentityProvider()
    if provider_id in _OVERLAY_IDENTITY:
        raise ProviderConfigError(
            f"identity provider {provider_id!r} is not bundled in the core "
            "(install the overlay adapter or set SANMAI_IDENTITY_PROVIDER=static)"
        )
    raise ProviderConfigError(f"unknown identity provider id: {provider_id!r}")


__all__ = ["build_llm", "build_storage", "build_identity"]
