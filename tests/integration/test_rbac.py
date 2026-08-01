"""RBAC integration tests for the DB-driven ``require_admin`` gate.

Exercises the trust anchor directly against the HR router: full_admin bypass, page-gated
allow/deny, non-admin -> 403, no-token -> 401, inactive admin -> 403, and venue scoping via
``admin_user_venues``. All identities are seeded into ``admin_users`` (authz is DB-driven,
not claim-driven) with :func:`tests.conftest.seed_admin_user`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from be.app.main import create_app
from be.db import get_session
from tests.conftest import admin_token, non_admin_token, seed_admin_user


@pytest.fixture
def app_client(sessionmaker_for, migrated_db):  # type: ignore[no-untyped-def]
    app = create_app()
    sm = sessionmaker_for

    async def _override_session() -> Any:
        async with sm() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    client = TestClient(app)
    try:
        yield app, client, migrated_db
    finally:
        app.dependency_overrides.clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_no_token_is_401(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    assert client.get("/api/v1/employees").status_code == 401
    # malformed scheme -> 401
    assert client.get(
        "/api/v1/employees", headers={"Authorization": "Token abc"}
    ).status_code == 401


def test_non_admin_token_is_403(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    # user-1 has no admin_users row -> 403 (claim `admin` alone does NOT grant)
    assert client.get("/api/v1/employees", headers=_auth(admin_token(uid="ghost"))).status_code == 403
    assert client.get("/api/v1/employees", headers=_auth(non_admin_token())).status_code == 403


def test_full_admin_bypasses_every_page(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    # the default seeded full_admin (uid admin-1) hits users/hr/roles/groups pages
    h = _auth(admin_token())
    assert client.get("/api/v1/admin/users", headers=h).status_code == 200
    assert client.get("/api/v1/employees", headers=h).status_code == 200
    assert client.get("/api/v1/job-roles", headers=h).status_code == 200
    assert client.get("/api/v1/employee-groups", headers=h).status_code == 200


def test_page_gated_admin_allow_and_deny(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    # an "hr"-only admin may read employees but not manage admin users
    seed_admin_user(url, admin_id=10, uid="hr-only", role="manager", allowed_pages=["hr"])
    h = _auth(admin_token(uid="hr-only"))
    assert client.get("/api/v1/employees", headers=h).status_code == 200  # "hr" page
    assert client.get("/api/v1/admin/users", headers=h).status_code == 403  # "users" page
    assert client.get("/api/v1/job-roles", headers=h).status_code == 403  # "roles" page


def test_email_keyed_admin_lookup(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    # no uid on the row — the caller matches by verified email claim instead
    seed_admin_user(url, admin_id=11, email="boss@example.com", role="full_admin")
    h = _auth(admin_token(uid="unmatched-uid", email="boss@example.com"))
    assert client.get("/api/v1/admin/users", headers=h).status_code == 200


def test_inactive_admin_is_403(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    seed_admin_user(url, admin_id=12, uid="disabled", role="full_admin", is_active=0)
    assert (
        client.get("/api/v1/employees", headers=_auth(admin_token(uid="disabled"))).status_code
        == 403
    )


def test_venue_scoping_allow_and_deny(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    # a page-gated admin confined to venue-A
    seed_admin_user(
        url,
        admin_id=13,
        uid="venue-admin",
        role="manager",
        allowed_pages=["hr"],
        venue_ids=["venue-A"],
    )
    # active venue comes from the token's principal.venues[0]
    ok = _auth(admin_token(uid="venue-admin", venues=["venue-A"]))
    assert client.get("/api/v1/employees", headers=ok).status_code == 200
    denied = _auth(admin_token(uid="venue-admin", venues=["venue-B"]))
    assert client.get("/api/v1/employees", headers=denied).status_code == 403


def test_admin_with_no_venue_grants_is_unrestricted(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    # single-venue-friendly default: no admin_user_venues rows -> any venue allowed
    seed_admin_user(url, admin_id=14, uid="anyvenue", role="manager", allowed_pages=["hr"])
    h = _auth(admin_token(uid="anyvenue", venues=["venue-Z"]))
    assert client.get("/api/v1/employees", headers=h).status_code == 200
