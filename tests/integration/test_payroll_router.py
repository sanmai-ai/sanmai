"""Integration tests for the payroll / compensation router via FastAPI TestClient.

Full request path — auth (StaticIdentityProvider -> DB-RBAC / verified identity), db
(migrated sqlite) — with zero external credentials. Focus:

* the CRITICAL-fix RBAC regression: 401 (no token) / 403 (non-admin) on EVERY payroll
  write (pay-rate, bonuses, payment-rules, scheduled-payments) — these were live
  UNAUTHENTICATED writes;
* admin happy paths (payment-rule CRUD, bonus create/list/delete, scheduled-payment
  create/list + calendar expansion);
* employee self-read ownership (own pay summary + own bonus log only);
* the bonus_task_log crediting double-pay guard + void-on-reopen (direct crud).
"""

from __future__ import annotations

import asyncio
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


def _admin_h() -> dict[str, str]:
    return _auth(admin_token())


def _make_employee(client: TestClient, name: str, email: str) -> int:
    r = client.post(
        "/api/v1/employees", headers=_admin_h(), json={"name": name, "email": email}
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _acrud(url: str, fn: str, **kw):  # type: ignore[no-untyped-def]
    from be.app.domains.payroll import crud
    from be.db import make_engine, make_sessionmaker

    sm = make_sessionmaker(make_engine(url))
    async with sm() as session:
        return await getattr(crud, fn)(session, **kw)


# ══════════════════════════════════════════════════════════════════
# RBAC regression — the CRITICAL fix (was UNAUTHENTICATED)
# ══════════════════════════════════════════════════════════════════


def test_payroll_writes_require_token(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    # No Authorization header -> 401 on every write.
    assert client.put("/api/v1/admin/employees/1/pay-rate", json={"pay_rate": 50}).status_code == 401
    assert client.post("/api/v1/admin/payment-rules", json={}).status_code == 401
    assert client.put("/api/v1/admin/payment-rules/1", json={}).status_code == 401
    assert client.delete("/api/v1/admin/payment-rules/1").status_code == 401
    assert client.post("/api/v1/admin/bonuses", json={}).status_code == 401
    assert client.delete("/api/v1/admin/bonuses/1").status_code == 401
    assert client.post("/api/v1/admin/scheduled-payments", json={}).status_code == 401
    assert client.get("/api/v1/admin/payment-rules").status_code == 401


def test_payroll_writes_reject_non_admin(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    # A page-limited admin WITHOUT the "payroll" page -> 403 (not full_admin).
    seed_admin_user(url, admin_id=20, uid="hr-only", role="manager", allowed_pages=["hr"])
    h = _auth(admin_token(uid="hr-only"))
    assert client.put("/api/v1/admin/employees/1/pay-rate", headers=h, json={"pay_rate": 50}).status_code == 403
    assert client.post("/api/v1/admin/payment-rules", headers=h, json={}).status_code == 403
    assert client.post("/api/v1/admin/bonuses", headers=h, json={"employee_id": 1, "year": 2026, "month": 7, "amount": 5}).status_code == 403
    assert client.post("/api/v1/admin/scheduled-payments", headers=h, json={"name": "Rent", "start_date": "2026-07-01"}).status_code == 403
    # A plain non-admin token -> 403 too.
    hn = _auth(non_admin_token(uid="ghost"))
    assert client.get("/api/v1/admin/payment-rules", headers=hn).status_code == 403


def test_payroll_page_admin_is_allowed(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    seed_admin_user(url, admin_id=21, uid="pay", role="manager", allowed_pages=["payroll"])
    h = _auth(admin_token(uid="pay"))
    assert client.get("/api/v1/admin/payment-rules", headers=h).status_code == 200


# ══════════════════════════════════════════════════════════════════
# ADMIN happy paths
# ══════════════════════════════════════════════════════════════════


def test_pay_rate_set_and_validated(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    eid = _make_employee(client, "Ada", "ada@example.com")
    h = _admin_h()
    r = client.put(
        f"/api/v1/admin/employees/{eid}/pay-rate",
        headers=h,
        json={"pay_type": "hourly", "pay_rate": 55},
    )
    assert r.status_code == 200 and r.json()["pay_rate"] == 55.0
    # bad pay_type -> 400
    assert client.put(
        f"/api/v1/admin/employees/{eid}/pay-rate", headers=h, json={"pay_type": "weekly"}
    ).status_code == 400
    # unknown employee -> 404
    assert client.put(
        "/api/v1/admin/employees/9999/pay-rate", headers=h, json={"pay_rate": 10}
    ).status_code == 404


def test_payment_rule_crud(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    h = _admin_h()
    # POST {} makes a draft rule (house convention).
    r = client.post("/api/v1/admin/payment-rules", headers=h, json={})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["active"] is True

    r = client.put(
        f"/api/v1/admin/payment-rules/{rid}",
        headers=h,
        json={"bonus_percent": 25, "day_of_week": 5, "after_minutes": 480},
    )
    assert r.status_code == 200 and r.json()["bonus_percent"] == 25

    # bad day_of_week -> 400
    assert client.post(
        "/api/v1/admin/payment-rules", headers=h, json={"day_of_week": 9}
    ).status_code == 400

    listing = client.get("/api/v1/admin/payment-rules", headers=h).json()
    assert any(x["id"] == rid for x in listing)

    assert client.delete(f"/api/v1/admin/payment-rules/{rid}", headers=h).status_code == 200
    assert client.delete(f"/api/v1/admin/payment-rules/{rid}", headers=h).status_code == 404


def test_bonus_create_list_delete(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    eid = _make_employee(client, "Ben", "ben@example.com")
    h = _admin_h()
    r = client.post(
        "/api/v1/admin/bonuses",
        headers=h,
        json={"employee_id": eid, "year": 2026, "month": 7, "amount": 250, "comment": "great"},
    )
    assert r.status_code == 201, r.text
    bid = r.json()["id"]

    # validation: missing employee_id -> 400; bad month -> 400
    assert client.post("/api/v1/admin/bonuses", headers=h, json={"year": 2026, "month": 7, "amount": 1}).status_code == 400
    assert client.post("/api/v1/admin/bonuses", headers=h, json={"employee_id": eid, "year": 2026, "month": 13, "amount": 1}).status_code == 400

    listing = client.get("/api/v1/admin/bonuses?year=2026&month=7", headers=h).json()
    assert any(b["id"] == bid and b["amount"] == 250.0 for b in listing)

    assert client.delete(f"/api/v1/admin/bonuses/{bid}", headers=h).status_code == 200
    assert client.delete(f"/api/v1/admin/bonuses/{bid}", headers=h).status_code == 404


def test_scheduled_payment_and_calendar(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    h = _admin_h()
    r = client.post(
        "/api/v1/admin/scheduled-payments",
        headers=h,
        json={
            "name": "Rent",
            "category": "rent",
            "payment_type": "mandatory",
            "amount": 5000,
            "is_recurring": True,
            "recurrence": {"freq": "monthly", "day": 1},
            "start_date": "2026-01-01",
        },
    )
    assert r.status_code == 201, r.text
    sp_id = r.json()["id"]
    assert r.json()["recurrence"] == {"freq": "monthly", "day": 1}

    # name required -> 400
    assert client.post(
        "/api/v1/admin/scheduled-payments", headers=h, json={"start_date": "2026-01-01"}
    ).status_code == 400

    cal = client.get(
        "/api/v1/admin/payment-calendar?from=2026-01-01&to=2026-03-31", headers=h
    ).json()
    rent_rows = [c for c in cal if c["scheduled_payment_id"] == sp_id]
    assert [c["due_date"] for c in rent_rows] == ["2026-01-01", "2026-02-01", "2026-03-01"]

    # override the Feb occurrence -> paid
    r = client.post(
        "/api/v1/admin/payment-occurrences/override",
        headers=h,
        json={"scheduled_payment_id": sp_id, "due_date": "2026-02-01", "status": "paid"},
    )
    assert r.status_code == 200
    cal = client.get(
        "/api/v1/admin/payment-calendar?from=2026-01-01&to=2026-03-31", headers=h
    ).json()
    feb = next(c for c in cal if c["scheduled_payment_id"] == sp_id and c["due_date"] == "2026-02-01")
    assert feb["status"] == "paid"


# ══════════════════════════════════════════════════════════════════
# STAFF self-read ownership
# ══════════════════════════════════════════════════════════════════


def test_staff_pay_summary_is_self_and_computable(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    eid = _make_employee(client, "Cara", "cara@example.com")
    client.put(
        f"/api/v1/admin/employees/{eid}/pay-rate",
        headers=_admin_h(),
        json={"pay_type": "hourly", "pay_rate": 40},
    )
    h = _auth(non_admin_token(uid="cara", email="cara@example.com"))
    r = client.get("/api/v1/staff/pay/2026/7", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["employee_id"] == eid
    assert body["computable"] is True
    assert body["total"] == 0.0  # no shifts this month

    # an unknown caller (no employee row) -> 404
    hx = _auth(non_admin_token(uid="nobody", email="nobody@example.com"))
    assert client.get("/api/v1/staff/pay/2026/7", headers=hx).status_code == 404


def test_staff_bonus_log_ownership(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    a = _make_employee(client, "Ann", "ann@example.com")
    _make_employee(client, "Bob", "bob@example.com")
    # credit a task bonus to Ann directly (the tasks-hook sink)
    asyncio.run(
        _acrud(
            url,
            "credit_task_bonus",
            venue_id="default",
            run_task_id=101,
            employee_id=a,
            amount=30.0,
            task_title="Deep clean",
        )
    )
    ha = _auth(non_admin_token(uid="ann", email="ann@example.com"))
    hb = _auth(non_admin_token(uid="bob", email="bob@example.com"))
    log_a = client.get("/api/v1/staff/bonus-log", headers=ha).json()
    log_b = client.get("/api/v1/staff/bonus-log", headers=hb).json()
    assert len(log_a) == 1 and log_a[0]["bonus_amount"] == 30.0
    assert log_b == []  # Bob sees none of Ann's rows


# ══════════════════════════════════════════════════════════════════
# bonus_task_log crediting — double-pay guard + void-on-reopen
# ══════════════════════════════════════════════════════════════════


def test_credit_bonus_is_idempotent_and_void_then_recredit(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    a = _make_employee(client, "Dee", "dee@example.com")

    first = asyncio.run(
        _acrud(url, "credit_task_bonus", venue_id="default", run_task_id=202, employee_id=a, amount=20.0)
    )
    assert first is not None
    # second credit for the same (run_task, employee) is a no-op (guard) -> None
    dup = asyncio.run(
        _acrud(url, "credit_task_bonus", venue_id="default", run_task_id=202, employee_id=a, amount=20.0)
    )
    assert dup is None

    admin_log = client.get("/api/v1/admin/bonus-task-log", headers=_admin_h()).json()
    live = [r for r in admin_log if r["run_task_id"] == 202 and not r["voided"]]
    assert len(live) == 1  # exactly one LIVE payout

    # reopen -> void, then re-complete -> a fresh live row is allowed again
    voided = asyncio.run(_acrud(url, "void_task_bonus", run_task_id=202, employee_id=a))
    assert voided == 1
    recredit = asyncio.run(
        _acrud(url, "credit_task_bonus", venue_id="default", run_task_id=202, employee_id=a, amount=20.0)
    )
    assert recredit is not None
    admin_log = client.get("/api/v1/admin/bonus-task-log", headers=_admin_h()).json()
    rows_202 = [r for r in admin_log if r["run_task_id"] == 202]
    assert sum(1 for r in rows_202 if not r["voided"]) == 1  # still one live
    assert sum(1 for r in rows_202 if r["voided"]) == 1  # one voided survives for audit


def test_mark_bonus_paid(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    a = _make_employee(client, "Eli", "eli@example.com")
    row = asyncio.run(
        _acrud(url, "credit_task_bonus", venue_id="default", run_task_id=303, employee_id=a, amount=15.0)
    )
    h = _admin_h()
    assert client.post(f"/api/v1/admin/bonus-task-log/{row['id']}/mark-paid", headers=h).status_code == 200
    assert client.post("/api/v1/admin/bonus-task-log/99999/mark-paid", headers=h).status_code == 404
    log = client.get("/api/v1/admin/bonus-task-log", headers=h).json()
    paid = next(r for r in log if r["id"] == row["id"])
    assert paid["paid"] is True
