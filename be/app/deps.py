"""FastAPI dependency wiring — the injection layer domains depend on.

Endpoints depend only on the vendor-neutral ABCs (:class:`LLMProvider`,
:class:`Storage`, :class:`IdentityProvider`), an :class:`AsyncSession`, and a
verified :class:`Principal` — never on a concrete vendor. Provider selection is
resolved from config by :mod:`be.app.providers`; the auth dependencies turn a
Bearer token into a :class:`Principal` via the identity seam and gate admin routes
on a claim.

``ProviderPermanent`` from ``verify_token`` (bad/expired token) maps to **401**;
a caller who is not an active admin (or lacks the requested page/venue) maps to **403**.

RBAC is **DB-driven**, not claim-driven: :func:`require_admin` verifies the token via
the :class:`IdentityProvider` seam, then resolves the resulting :class:`Principal`
(``uid`` / ``email`` claim) against the ``admin_users`` table. This keeps authorization
IdentityProvider-agnostic — a token claim carries identity, never authority — and lets
the HR domain own the trust anchor (see ``migrations/0004_hr.sql``).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from be.adapters.errors import ProviderPermanent
from be.adapters.identity.base import IdentityProvider
from be.adapters.llm.base import LLMProvider
from be.adapters.notify.base import Notifier
from be.adapters.storage.base import Storage
from be.adapters.types import Principal
from be.app.providers import build_identity, build_llm, build_notify, build_storage
from be.config import Settings, get_settings
from be.db import get_session

__all__ = [
    "get_settings_dep",
    "get_llm",
    "get_storage",
    "get_notify",
    "get_identity",
    "get_session",
    "require_principal",
    "require_admin",
    "authorize_admin",
    "get_venue_id",
    "AdminContext",
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


def get_notify(settings: SettingsDep) -> Notifier:
    """Bind the configured :class:`Notifier` for this request (best-effort seam)."""
    return build_notify(settings)


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


def get_venue_id(principal: PrincipalDep, settings: SettingsDep) -> str:
    """Resolve the caller's active venue: first scoped venue, else the default.

    Company data is venue-published; today every write lands under one venue, so we
    thread ``Principal.venues[0]`` when present and fall back to
    ``settings.default_venue_id``. This keeps the domains venue_id-ready without
    building multi-venue routing yet.
    """
    if principal.venues:
        return principal.venues[0]
    return settings.default_venue_id


VenueDep = Annotated[str, Depends(get_venue_id)]


@dataclass(frozen=True)
class AdminContext:
    """An authorized admin caller: verified identity + resolved DB-RBAC grants.

    ``uid`` is kept as the first field so any consumer that only reads ``admin.uid``
    (menu/inventory routers) is unaffected by the claim-check -> DB-RBAC upgrade.
    """

    uid: str
    email: str | None
    role: str
    allowed_pages: list[str]
    venues: list[str] = field(default_factory=list)
    principal: Principal | None = None


# Resolve the verified Principal against the admin_users trust anchor. Match by uid OR
# email claim (either may be the login identity). allowed_pages is JSON-as-TEXT.
_ADMIN_LOOKUP = text(
    """
    SELECT id, uid, email, role, allowed_pages
    FROM admin_users
    WHERE is_active = 1
      AND (
        uid = CAST(:uid AS TEXT)
        OR (CAST(:email AS TEXT) IS NOT NULL AND email = CAST(:email AS TEXT))
      )
    ORDER BY id
    """
)

_ADMIN_VENUES = text(
    "SELECT venue_id FROM admin_user_venues WHERE admin_user_id = :admin_user_id"
)


def _parse_pages(value: Any) -> list[str]:
    """Parse the JSON-as-TEXT allowed_pages column into a list of page keys."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    return []


async def authorize_admin(
    session: AsyncSession,
    principal: Principal,
    venue_id: str,
    *pages: str,
) -> AdminContext:
    """Resolve a verified :class:`Principal` against ``admin_users`` (the trust anchor).

    Shared by :func:`require_admin` and by routes that authorize outside the normal
    dependency path (e.g. the analytics upload's admin-OR-shared-secret gate). Raises
    **403** when the caller is not an active admin, lacks a required page, or acts in an
    ungranted venue.
    """
    email = principal.claims.get("email")
    row = (
        await session.execute(_ADMIN_LOOKUP, {"uid": principal.uid, "email": email})
    ).mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin privilege required",
        )

    role = str(row["role"])
    allowed = _parse_pages(row["allowed_pages"])

    if pages and role != "full_admin" and not any(p in allowed for p in pages):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"admin access to '{pages[0]}' required",
        )

    if role != "full_admin":
        grants = [
            r["venue_id"]
            for r in (
                await session.execute(_ADMIN_VENUES, {"admin_user_id": row["id"]})
            ).mappings().all()
        ]
        if grants and venue_id not in grants:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="admin access to this venue is not granted",
            )

    return AdminContext(
        uid=principal.uid,
        email=email,
        role=role,
        allowed_pages=allowed,
        venues=list(principal.venues),
        principal=principal,
    )


def require_admin(
    *pages: str,
) -> Callable[[Principal, AsyncSession, str], Awaitable[AdminContext]]:
    """Factory -> a dependency that authorizes the caller against ``admin_users``.

    * The token is verified via the :class:`IdentityProvider` seam (bad/expired -> 401).
    * The resulting :class:`Principal` is looked up in ``admin_users`` by uid/email;
      no active row -> **403** (this — not a token claim — is what makes a caller admin).
    * Page gate: ``full_admin`` bypasses; otherwise the caller must hold at least one of
      *pages* in ``allowed_pages`` (no *pages* = "any active admin") -> else **403**.
    * Venue gate (single-venue-friendly): ``full_admin`` bypasses; a caller with venue
      grants may only act in a granted venue; a caller with **no** grants is unrestricted.

    Called with no args at import time to build :data:`AdminDep`; it must not touch the
    DB until the returned dependency runs.
    """

    async def _dep(
        principal: PrincipalDep,
        session: DbDep,
        venue_id: VenueDep,
    ) -> AdminContext:
        return await authorize_admin(session, principal, venue_id, *pages)

    return _dep


# Page-less gate: any active admin. Menu + inventory routers consume this verbatim
# (they only read ``admin.uid``), so the claim-check -> DB-RBAC upgrade needs no edits.
AdminDep = Annotated[AdminContext, Depends(require_admin())]
