"""Shared test fixtures — zero external credentials.

Sets the fail-loud ``SANMAI_*`` env so :func:`be.config.get_settings` constructs with
the fake providers (echo/local/static), and provides sqlite-backed helpers: a migrated
database (via the real :mod:`be.migrate` runner) plus an ``async_sessionmaker`` and a
FastAPI ``TestClient`` whose ``get_session`` is bound to that database.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

# --- fail-loud config: fake providers, no creds (set before be.config is imported) ---
os.environ.setdefault("SANMAI_CONFIG_SCHEMA_VERSION", "1")
os.environ.setdefault("SANMAI_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SANMAI_ENV", "test")
os.environ.setdefault("SANMAI_PAYMENTS_PROVIDER", "demo")
os.environ.setdefault("SANMAI_LLM_PROVIDER", "echo")
os.environ.setdefault("SANMAI_FISCAL_PROFILE", "generic")
os.environ.setdefault("SANMAI_STORAGE_PROVIDER", "local")
os.environ.setdefault("SANMAI_IDENTITY_PROVIDER", "static")
os.environ.setdefault("SANMAI_DEFAULT_VENUE_ID", "default")

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def _mint_token(uid: str, claims: dict, venues: list[str] | None) -> str:
    payload = {"uid": uid, "claims": claims, "venues": venues or []}
    raw = json.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def admin_token(
    uid: str = "admin-1",
    venues: list[str] | None = None,
    email: str | None = None,
    phone: str | None = None,
) -> str:
    """Mint a base64url static-identity token for an admin caller.

    Authorization is now DB-driven (``admin_users``), so the token only carries identity:
    the ``admin`` claim is kept for backward-compat but is NOT what grants access — a
    matching active ``admin_users`` row (seeded via ``migrated_db`` / ``seed_admin_user``)
    is. ``email``/``phone`` populate the claims used for DB lookup and ``/staff/me``.
    """
    claims: dict = {"admin": True}
    if email is not None:
        claims["email"] = email
    if phone is not None:
        claims["phone"] = phone
    return _mint_token(uid, claims, venues)


def non_admin_token(
    uid: str = "user-1", email: str | None = None, phone: str | None = None
) -> str:
    claims: dict = {}
    if email is not None:
        claims["email"] = email
    if phone is not None:
        claims["phone"] = phone
    return _mint_token(uid, claims, None)


def seed_admin_user(
    url: str,
    *,
    admin_id: int,
    uid: str | None = None,
    email: str | None = None,
    role: str = "full_admin",
    allowed_pages: list[str] | None = None,
    is_active: int = 1,
    venue_ids: list[str] | None = None,
) -> None:
    """Insert an ``admin_users`` row (+ optional venue grants) into the migrated sqlite db.

    Uses a short-lived sync engine so RBAC tests can seed page-limited / venue-scoped /
    inactive admins directly, without going through the (gated) admin-management API.
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO admin_users (id, venue_id, uid, email, role, "
                    "allowed_pages, is_active, telegram_user_id, created_at) VALUES "
                    "(:id, 'default', :uid, :email, :role, :pages, :active, NULL, :ts)"
                ),
                {
                    "id": admin_id,
                    "uid": uid,
                    "email": email,
                    "role": role,
                    "pages": json.dumps(allowed_pages or []),
                    "active": is_active,
                    "ts": "2026-01-01T00:00:00+00:00",
                },
            )
            for v in venue_ids or []:
                conn.execute(
                    text(
                        "INSERT INTO admin_user_venues (admin_user_id, venue_id) "
                        "VALUES (:aid, :v)"
                    ),
                    {"aid": admin_id, "v": v},
                )
    finally:
        engine.dispose()


@pytest.fixture
def migrated_db(tmp_path: Path) -> str:
    """Create a fresh sqlite file, apply all migrations, seed the default full_admin.

    The seeded ``admin_users`` row (uid ``admin-1``, ``full_admin``) is the DB-RBAC
    counterpart of :func:`admin_token`'s default identity, so every menu/inventory router
    test that authenticates with ``admin_token()`` stays green under the new require_admin.
    """
    from be import migrate

    db_path = tmp_path / "menu.db"
    url = f"sqlite:///{db_path}"
    migrate.run(url, MIGRATIONS_DIR)
    seed_admin_user(
        url, admin_id=1, uid="admin-1", email="admin@example.test", role="full_admin"
    )
    return url


@pytest.fixture
def sessionmaker_for(migrated_db: str):  # type: ignore[no-untyped-def]
    """An ``async_sessionmaker`` bound to the migrated sqlite database."""
    from be.db import make_engine, make_sessionmaker

    engine = make_engine(migrated_db)
    return make_sessionmaker(engine)
