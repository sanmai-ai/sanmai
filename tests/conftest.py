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


def admin_token(uid: str = "admin-1", venues: list[str] | None = None) -> str:
    """Mint a base64url static-identity token carrying the ``admin`` claim."""
    payload = {"uid": uid, "claims": {"admin": True}, "venues": venues or []}
    raw = json.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def non_admin_token(uid: str = "user-1") -> str:
    payload = {"uid": uid, "claims": {}, "venues": []}
    raw = json.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


@pytest.fixture
def migrated_db(tmp_path: Path) -> str:
    """Create a fresh sqlite file and apply all repo migrations via be.migrate.run."""
    from be import migrate

    db_path = tmp_path / "menu.db"
    url = f"sqlite:///{db_path}"
    migrate.run(url, MIGRATIONS_DIR)
    return url


@pytest.fixture
def sessionmaker_for(migrated_db: str):  # type: ignore[no-untyped-def]
    """An ``async_sessionmaker`` bound to the migrated sqlite database."""
    from be.db import make_engine, make_sessionmaker

    engine = make_engine(migrated_db)
    return make_sessionmaker(engine)
