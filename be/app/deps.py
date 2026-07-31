"""FastAPI dependency wiring — the injection layer domains depend on.

Endpoints depend only on the vendor-neutral ABCs (:class:`LLMProvider`,
:class:`Storage`, :class:`IdentityProvider`), an :class:`AsyncSession`, and a
verified :class:`Principal` — never on a concrete vendor. Provider selection is
resolved from config by :mod:`be.app.providers`; the auth dependencies turn a
Bearer token into a :class:`Principal` via the identity seam and gate admin routes
on a claim.

``ProviderPermanent`` from ``verify_token`` (bad/expired token) maps to **401**;
a missing admin claim maps to **403**.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from be.adapters.errors import ProviderPermanent
from be.adapters.identity.base import IdentityProvider
from be.adapters.llm.base import LLMProvider
from be.adapters.storage.base import Storage
from be.adapters.types import Principal
from be.app.providers import build_identity, build_llm, build_storage
from be.config import Settings, get_settings
from be.db import get_session

__all__ = [
    "get_settings_dep",
    "get_llm",
    "get_storage",
    "get_identity",
    "get_session",
    "require_principal",
    "require_admin",
    "get_venue_id",
    "SettingsDep",
    "PrincipalDep",
    "AdminDep",
    "VenueDep",
    "DbDep",
]

DbDep = Annotated[AsyncSession, Depends(get_session)]


def get_settings_dep() -> Settings:
    """Dependency exposing the process-wide :class:`Settings`."""
    return get_settings()


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def get_llm(settings: SettingsDep) -> LLMProvider:
    """Bind the configured :class:`LLMProvider` for this request."""
    return build_llm(settings)


def get_storage(settings: SettingsDep) -> Storage:
    """Bind the configured :class:`Storage` for this request."""
    return build_storage(settings)


def get_identity(settings: SettingsDep) -> IdentityProvider:
    """Bind the configured :class:`IdentityProvider` for this request."""
    return build_identity(settings)


def _extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must be 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token.strip()


def require_principal(
    identity: Annotated[IdentityProvider, Depends(get_identity)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Verify the Bearer token and return the authenticated :class:`Principal`.

    A bad/expired token (``ProviderPermanent``) becomes HTTP 401.
    """
    token = _extract_bearer(authorization)
    try:
        return identity.verify_token(token)
    except ProviderPermanent as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


PrincipalDep = Annotated[Principal, Depends(require_principal)]


def require_admin(principal: PrincipalDep) -> Principal:
    """Gate admin routes on the ``admin`` claim; missing claim -> HTTP 403."""
    if not principal.claims.get("admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin privilege required",
        )
    return principal


AdminDep = Annotated[Principal, Depends(require_admin)]


def get_venue_id(principal: PrincipalDep, settings: SettingsDep) -> str:
    """Resolve the caller's active venue: first scoped venue, else the default.

    Menu is company-owned/venue-published; today every write lands under one venue,
    so we thread ``Principal.venues[0]`` when present and fall back to
    ``settings.default_venue_id``. This keeps the domain venue_id-ready without
    building multi-venue routing yet.
    """
    if principal.venues:
        return principal.venues[0]
    return settings.default_venue_id


VenueDep = Annotated[str, Depends(get_venue_id)]
