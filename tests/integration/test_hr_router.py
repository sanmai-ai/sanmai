"""Integration tests for the HR router via FastAPI TestClient.

Full request path — auth (StaticIdentityProvider -> DB-RBAC), db (migrated sqlite) — with
zero external credentials. Covers employee/admin-user/role/group CRUD, membership replaces,
``/staff/me`` self-identity, and ``/admin/access``. ``get_session`` is bound to a per-test
migrated sqlite db (which also seeds the default ``full_admin`` uid ``admin-1``).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from be.app.main import create_app
from be.db import get_session
from tests.conftest import admin_token, non_admin_token


@pytest.fixture
def app_client(sessionmaker_for):  # type: ignore[no-untyped-def]
    app = create_app()
    sm = sessionmaker_for

    async def _override_session() -> Any:
        async with sm() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    client = TestClient(app)
    try:
        yield app, client
    finally:
        app.dependency_overrides.clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- employees --------------------------------------------------------------


def test_employee_create_list_update_and_dupes(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client = app_client
    h = _auth(admin_token())

    # POST {} rejected — name + one identity required
    assert client.post("/api/v1/employees", headers=h, json={}).status_code == 400

    r = client.post(
        "/api/v1/employees",
        headers=h,
        json={"name": "Ada", "surname": "Byte", "email": "Ada@Example.com", "pay_rate": 40},
    )
    assert r.status_code == 201, r.text
    emp = r.json()
    assert emp["email"] == "ada@example.com"  # normalized
    eid = emp["id"]

    # duplicate email -> 409
    r = client.post(
        "/api/v1/employees", headers=h, json={"name": "Ada2", "email": "ada@example.com"}
    )
    assert r.status_code == 409

    # invalid pay_type -> 400
    r = client.post(
        "/api/v1/employees",
        headers=h,
        json={"name": "Bob", "phone": "0551234567", "pay_type": "weekly"},
    )
    assert r.status_code == 400

    listing = client.get("/api/v1/employees", headers=h).json()
    assert any(e["id"] == eid for e in listing)

    # update
    r = client.put(
        f"/api/v1/employees/{eid}",
        headers=h,
        json={"name": "Ada", "surname": "Lovelace", "email": "ada@example.com"},
    )
    assert r.status_code == 200 and r.json()["surname"] == "Lovelace"
    # update missing -> 404
    assert (
        client.put(
            "/api/v1/employees/9999", headers=h, json={"name": "X", "email": "x@y.z"}
        ).status_code
        == 404
    )


def test_employee_groups_roles_and_membership(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client = app_client
    h = _auth(admin_token())

    # create a group + role
    g = client.post("/api/v1/employee-groups", headers=h, json={"name": "Kitchen"}).json()
    role = client.post(
        "/api/v1/job-roles", headers=h, json={"name": "Cook", "start_time": "9:05"}
    ).json()
    assert role["start_time"] == "09:05"  # normalized

    # bad role time -> 400
    assert (
        client.post(
            "/api/v1/job-roles", headers=h, json={"name": "Bad", "start_time": "nope"}
        ).status_code
        == 400
    )

    emp = client.post(
        "/api/v1/employees",
        headers=h,
        json={
            "name": "Cara",
            "email": "cara@example.com",
            "default_role_id": role["id"],
            "group_ids": [g["id"]],
        },
    ).json()
    assert emp["group_ids"] == [g["id"]]

    # unknown group on create -> 400
    assert (
        client.post(
            "/api/v1/employees",
            headers=h,
            json={"name": "Dee", "email": "dee@example.com", "group_ids": [9999]},
        ).status_code
        == 400
    )

    # employee appears with role + group names
    listing = client.get("/api/v1/employees", headers=h).json()
    row = next(e for e in listing if e["id"] == emp["id"])
    assert row["default_role_name"] == "Cook"
    assert row["groups"][0]["name"] == "Kitchen"

    # replace-all group membership via /employees/{id}/groups
    r = client.put(
        f"/api/v1/employees/{emp['id']}/groups", headers=h, json={"group_ids": []}
    )
    assert r.status_code == 200 and r.json()["group_ids"] == []

    # group members endpoint reflects membership, then set-members
    client.put(
        f"/api/v1/employee-groups/{g['id']}/members",
        headers=h,
        json={"employee_ids": [emp["id"]]},
    )
    members = client.get(
        f"/api/v1/employee-groups/{g['id']}/members", headers=h
    ).json()
    assert members[0]["id"] == emp["id"]
    groups = client.get("/api/v1/employee-groups", headers=h).json()
    assert next(x for x in groups if x["id"] == g["id"])["member_count"] == 1

    # update role deactivate
    r = client.put(f"/api/v1/job-roles/{role['id']}", headers=h, json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False


# --- admin users ------------------------------------------------------------


def test_admin_user_management_and_managed_employees(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client = app_client
    h = _auth(admin_token())

    # POST {} rejected — needs a login identity
    assert client.post("/api/v1/admin/users", headers=h, json={}).status_code == 400

    r = client.post(
        "/api/v1/admin/users",
        headers=h,
        json={"email": "Manager@Example.com", "role": "manager", "allowed_pages": ["hr"]},
    )
    assert r.status_code == 200, r.text
    mgr = r.json()
    assert mgr["email"] == "manager@example.com"
    assert mgr["allowed_pages"] == ["hr"]

    # duplicate email -> 409
    assert (
        client.post(
            "/api/v1/admin/users", headers=h, json={"email": "manager@example.com"}
        ).status_code
        == 409
    )

    listing = client.get("/api/v1/admin/users", headers=h).json()
    assert any(u["id"] == mgr["id"] for u in listing)

    # create an employee to manage
    emp = client.post(
        "/api/v1/employees", headers=h, json={"name": "Eve", "email": "eve@example.com"}
    ).json()
    r = client.put(
        f"/api/v1/admin/users/{mgr['id']}/employees",
        headers=h,
        json={"employee_ids": [emp["id"]]},
    )
    assert r.status_code == 200 and r.json()["employee_ids"] == [emp["id"]]
    managed = client.get(f"/api/v1/admin/users/{mgr['id']}/employees", headers=h).json()
    assert managed[0]["id"] == emp["id"]

    # update role/pages
    r = client.put(
        f"/api/v1/admin/users/{mgr['id']}", headers=h, json={"allowed_pages": ["hr", "roles"]}
    )
    assert r.status_code == 200 and set(r.json()["allowed_pages"]) == {"hr", "roles"}

    # delete
    assert client.delete(f"/api/v1/admin/users/{mgr['id']}", headers=h).status_code == 200
    assert client.delete(f"/api/v1/admin/users/{mgr['id']}", headers=h).status_code == 404


# --- self identity ----------------------------------------------------------


def test_staff_me_resolves_from_verified_token(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client = app_client
    h = _auth(admin_token())
    client.post(
        "/api/v1/employees",
        headers=h,
        json={"name": "Sam", "surname": "Fox", "email": "sam@example.com"},
    )

    # a staff (non-admin) token carrying the verified email resolves the employee
    staff_h = _auth(non_admin_token(uid="staff-1", email="sam@example.com"))
    r = client.get("/api/v1/staff/me", headers=staff_h)
    assert r.status_code == 200, r.text
    assert r.json() == {"name": "Sam", "full_name": "Sam Fox"}

    # unknown identity -> 404
    r = client.get(
        "/api/v1/staff/me", headers=_auth(non_admin_token(email="ghost@example.com"))
    )
    assert r.status_code == 404

    # no bearer -> 401
    assert client.get("/api/v1/staff/me").status_code == 401


def test_admin_access_reports_role_pages_and_managed(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client = app_client
    # default seeded full_admin
    r = client.get("/api/v1/admin/access", headers=_auth(admin_token()))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "full_admin"
    assert body["managed_employee_ids"] == []

    # non-admin -> 403
    assert client.get("/api/v1/admin/access", headers=_auth(non_admin_token())).status_code == 403
