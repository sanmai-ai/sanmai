"""Integration tests for the scheduling / timesheet / time-off router.

Full request path — auth (StaticIdentityProvider -> DB-RBAC / verified-identity), db
(migrated sqlite) — with zero external credentials. Focus: admin-vs-staff access,
ownership enforcement (staff A can't touch staff B), 401/403, venue scoping, the
edited-after-approval bounce, the recurring merge + time-off tombstone suppression,
and the timesheet correction lifecycle.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from be.app.main import create_app
from be.db import get_session
from tests.conftest import admin_token, non_admin_token, seed_admin_user

WEEK = "2024-01-07"  # a Sunday
MON = "2024-01-08"  # Monday of that week
RANGE = {"from": "2024-01-07", "to": "2024-01-13"}


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


def _staff_h(email: str, uid: str = "staff") -> dict[str, str]:
    return _auth(non_admin_token(uid=uid, email=email))


def _seed_employees_and_role(client: TestClient) -> tuple[int, int, int]:
    """Create employees A + B and a role via the (full_admin) hr endpoints."""
    h = _admin_h()
    a = client.post(
        "/api/v1/employees", headers=h, json={"name": "Aya", "email": "a@example.com"}
    ).json()
    b = client.post(
        "/api/v1/employees", headers=h, json={"name": "Ben", "email": "b@example.com"}
    ).json()
    role = client.post(
        "/api/v1/job-roles", headers=h, json={"name": "Cook", "start_time": "09:00"}
    ).json()
    return a["id"], b["id"], role["id"]


def _make_pending_shift(client: TestClient, emp_id: int, role_id: int) -> int:
    """Admin: create week, assign a shift to *emp_id*, publish -> 'pending'."""
    h = _admin_h()
    week = client.post("/api/v1/schedule/weeks", headers=h, params={"week_start": WEEK}).json()
    shift = client.post(
        f"/api/v1/schedule/weeks/{week['id']}/shifts",
        headers=h,
        json={
            "employee_id": emp_id,
            "role_id": role_id,
            "shift_date": MON,
            "start_time": "09:00",
            "end_time": "17:00",
        },
    ).json()
    client.post(f"/api/v1/schedule/weeks/{week['id']}/publish", headers=h)
    return shift["id"]


# --- access control ---------------------------------------------------------


def test_no_token_is_401(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    assert client.get("/api/v1/schedule/weeks").status_code == 401
    assert client.get("/api/v1/schedule/my-shifts", params=RANGE).status_code == 401


def test_non_admin_hitting_admin_route_is_403(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    _seed_employees_and_role(client)
    # a real employee token (not an admin_users row) -> 403 on an admin route
    assert client.get("/api/v1/schedule/weeks", headers=_staff_h("a@example.com")).status_code == 403
    assert (
        client.get("/api/v1/admin/time-off", headers=_staff_h("a@example.com")).status_code == 403
    )


def test_page_gated_admin_schedule_vs_timesheets(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    seed_admin_user(url, admin_id=20, uid="sched", role="manager", allowed_pages=["schedule"])
    seed_admin_user(url, admin_id=21, uid="ts", role="manager", allowed_pages=["timesheets"])
    sched_h = _auth(admin_token(uid="sched"))
    ts_h = _auth(admin_token(uid="ts"))
    # schedule-only admin: schedule OK, timesheets denied
    assert client.get("/api/v1/schedule/weeks", headers=sched_h, params=RANGE).status_code == 200
    assert client.get("/api/v1/admin/time-off", headers=sched_h).status_code == 403
    # timesheets-only admin: timesheets OK, schedule denied
    assert client.get("/api/v1/admin/time-off", headers=ts_h).status_code == 200
    assert client.get("/api/v1/schedule/weeks", headers=ts_h, params=RANGE).status_code == 403


# --- ownership: shifts ------------------------------------------------------


def test_staff_can_only_act_on_own_shift(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    a_id, _b_id, role_id = _seed_employees_and_role(client)
    shift_id = _make_pending_shift(client, a_id, role_id)

    # B cannot approve / decline / request-change A's shift
    b = _staff_h("b@example.com", uid="staff-b")
    assert client.post(f"/api/v1/schedule/shifts/{shift_id}/approve", headers=b).status_code == 400
    assert client.post(f"/api/v1/schedule/shifts/{shift_id}/decline", headers=b).status_code == 400
    rc = client.post(
        f"/api/v1/schedule/shifts/{shift_id}/request-change",
        headers=b,
        json={"requested_start_time": "10:00", "requested_end_time": "18:00"},
    )
    assert rc.status_code == 403

    # B's my-shifts is empty; A's shows the shift
    assert client.get("/api/v1/schedule/my-shifts", headers=b, params=RANGE).json() == []
    a = _staff_h("a@example.com", uid="staff-a")
    a_shifts = client.get("/api/v1/schedule/my-shifts", headers=a, params=RANGE).json()
    assert len(a_shifts) == 1 and a_shifts[0]["id"] == shift_id

    # A can approve own shift
    assert client.post(f"/api/v1/schedule/shifts/{shift_id}/approve", headers=a).status_code == 200


def test_edited_after_approval_bounces_to_pending(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    a_id, _b_id, role_id = _seed_employees_and_role(client)
    shift_id = _make_pending_shift(client, a_id, role_id)
    a = _staff_h("a@example.com", uid="staff-a")
    client.post(f"/api/v1/schedule/shifts/{shift_id}/approve", headers=a)

    # admin materially edits the approved shift -> bounced to pending + flagged
    r = client.put(
        f"/api/v1/schedule/shifts/{shift_id}",
        headers=_admin_h(),
        json={"start_time": "10:00"},
    )
    assert r.status_code == 200
    week = client.get("/api/v1/schedule/weeks", headers=_admin_h(), params=RANGE).json()[0]
    shifts = client.get(
        f"/api/v1/schedule/weeks/{week['id']}/shifts", headers=_admin_h()
    ).json()
    row = next(s for s in shifts if s["id"] == shift_id)
    assert row["status"] == "pending"
    assert row["edited_after_approval"] is True


# --- ownership: availability ------------------------------------------------


def test_availability_is_per_employee(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    _seed_employees_and_role(client)
    a = _staff_h("a@example.com", uid="staff-a")
    b = _staff_h("b@example.com", uid="staff-b")

    assert client.put(
        "/api/v1/schedule/availability",
        headers=a,
        json=[{"day_of_week": 1, "is_available": False, "note": "class"}],
    ).status_code == 200

    a_av = client.get("/api/v1/schedule/availability", headers=a).json()
    assert len(a_av) == 1 and a_av[0]["is_available"] is False
    # B never set anything -> empty (A's rows are not visible)
    assert client.get("/api/v1/schedule/availability", headers=b).json() == []

    # upsert: same day flips back
    client.put(
        "/api/v1/schedule/availability",
        headers=a,
        json=[{"day_of_week": 1, "is_available": True}],
    )
    a_av = client.get("/api/v1/schedule/availability", headers=a).json()
    assert len(a_av) == 1 and a_av[0]["is_available"] is True


# --- ownership: time off ----------------------------------------------------


def test_time_off_ownership(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    _seed_employees_and_role(client)
    a = _staff_h("a@example.com", uid="staff-a")
    b = _staff_h("b@example.com", uid="staff-b")

    created = client.post(
        "/api/v1/staff/time-off",
        headers=a,
        json={"date_from": MON, "date_to": MON, "note": "trip"},
    )
    assert created.status_code == 200
    tid = created.json()["id"]

    # A sees own; B does not (own list is employee-scoped)
    assert len(client.get("/api/v1/staff/time-off", headers=a, params=RANGE).json()) == 1
    assert client.get("/api/v1/staff/time-off", headers=b, params=RANGE).json() == []

    # B's team view sees A's request but privacy-neutralized (no note/type/id)
    team = client.get("/api/v1/staff/time-off/team", headers=b, params=RANGE).json()
    assert len(team) == 1
    assert set(team[0]) == {
        "employee_name",
        "kind",
        "label",
        "status",
        "date_from",
        "date_to",
        "requested_on",
    }

    # B cannot delete A's request; A can
    assert client.delete(f"/api/v1/staff/time-off/{tid}", headers=b).status_code == 404
    assert client.delete(f"/api/v1/staff/time-off/{tid}", headers=a).status_code == 200


# --- venue scoping ----------------------------------------------------------


def test_weeks_are_venue_scoped(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    # full_admin bypasses the venue gate; the write lands under the token's venue.
    va = _auth(admin_token(venues=["venue-A"]))
    client.post("/api/v1/schedule/weeks", headers=va, params={"week_start": WEEK})

    # default-venue admin does not see venue-A's week; venue-A admin does
    default_weeks = client.get("/api/v1/schedule/weeks", headers=_admin_h(), params=RANGE).json()
    assert default_weeks == []
    va_weeks = client.get("/api/v1/schedule/weeks", headers=va, params=RANGE).json()
    assert len(va_weeks) == 1 and va_weeks[0]["week_start"] == WEEK


# --- timesheet correction lifecycle ----------------------------------------


def test_report_hours_correction_flow(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    _seed_employees_and_role(client)
    a = _staff_h("a@example.com", uid="staff-a")
    b = _staff_h("b@example.com", uid="staff-b")

    rep = client.post(
        "/api/v1/staff/timesheet/report-hours",
        headers=a,
        json={"shift_date": MON, "requested_start_time": "09:00", "requested_end_time": "17:00"},
    )
    assert rep.status_code == 200
    shift_id = rep.json()["shift_id"]

    # B cannot file a correction on A's shift
    assert client.post(
        "/api/v1/staff/timesheet/correction",
        headers=b,
        json={"shift_id": shift_id, "requested_start_time": "10:00"},
    ).status_code == 403

    # A's timesheet: one shift, blocked by the pending correction report-hours filed
    ts = client.get("/api/v1/staff/timesheet/2024/1", headers=a).json()
    assert len(ts["shifts"]) == 1
    assert ts["has_blocking_pending"] is True
    assert ts["total_hours"] == "8h 0m"

    # timesheets-admin lists + approves the correction
    admin_h = _admin_h()
    corr = client.get("/api/v1/admin/timesheet-corrections", headers=admin_h).json()
    assert len(corr) == 1 and corr[0]["shift_id"] == shift_id
    cid = corr[0]["id"]
    assert client.post(
        f"/api/v1/admin/timesheet-corrections/{cid}/approve", headers=admin_h
    ).status_code == 200

    # no longer blocking -> confirmable; A confirms the month
    ts = client.get("/api/v1/staff/timesheet/2024/1", headers=a).json()
    assert ts["has_blocking_pending"] is False
    assert ts["unconfirmed_confirmable"] == 1
    conf = client.post(
        "/api/v1/staff/timesheet/confirm", headers=a, json={"year": 2024, "month": 1}
    )
    assert conf.status_code == 200 and conf.json()["confirmed"] == 1
    ts = client.get("/api/v1/staff/timesheet/2024/1", headers=a).json()
    assert ts["confirmed"] is True


# --- recurring merge + time-off tombstone ----------------------------------


def test_recurring_merge_and_timeoff_suppression(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    a_id, _b_id, role_id = _seed_employees_and_role(client)
    admin_h = _admin_h()
    # make A stable so recurring shifts synthesize into my-shifts
    client.put(
        f"/api/v1/employees/{a_id}",
        headers=admin_h,
        json={"name": "Aya", "email": "a@example.com", "is_stable_schedule": True},
    )
    # recurring Monday shift, effective from before the test window
    client.post(
        "/api/v1/schedule/recurring-shifts",
        headers=admin_h,
        json={
            "employee_id": a_id,
            "day_of_week": 1,
            "role_id": role_id,
            "start_time": "09:00",
            "end_time": "17:00",
            "start_date": "2024-01-01",
        },
    )
    a = _staff_h("a@example.com", uid="staff-a")
    shifts = client.get("/api/v1/schedule/my-shifts", headers=a, params=RANGE).json()
    stable = [s for s in shifts if s["status"] == "stable"]
    assert len(stable) == 1 and stable[0]["shift_date"] == MON

    # approve time-off on the recurring day -> cancelled tombstone suppresses it
    to = client.post(
        "/api/v1/staff/time-off", headers=a, json={"date_from": MON, "date_to": MON}
    ).json()
    client.post(
        f"/api/v1/admin/time-off/{to['id']}/respond",
        headers=admin_h,
        json={"status": "approved"},
    )
    shifts = client.get("/api/v1/schedule/my-shifts", headers=a, params=RANGE).json()
    assert [s for s in shifts if s["shift_date"] == MON] == []


# --- admin change-request approval ------------------------------------------


def test_change_request_approve_flow(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    a_id, _b_id, role_id = _seed_employees_and_role(client)
    shift_id = _make_pending_shift(client, a_id, role_id)
    a = _staff_h("a@example.com", uid="staff-a")

    # A requests a time change on their own shift
    rc = client.post(
        f"/api/v1/schedule/shifts/{shift_id}/request-change",
        headers=a,
        json={"requested_start_time": "10:00", "requested_end_time": "18:00", "reason": "bus"},
    )
    assert rc.status_code == 200

    # schedule-admin sees + approves it -> shift times updated
    admin_h = _admin_h()
    reqs = client.get("/api/v1/schedule/change-requests", headers=admin_h).json()
    assert len(reqs) == 1
    rid = reqs[0]["id"]
    assert client.post(
        f"/api/v1/schedule/change-requests/{rid}/admin-approve", headers=admin_h
    ).status_code == 200
    week = client.get("/api/v1/schedule/weeks", headers=admin_h, params=RANGE).json()[0]
    row = next(
        s
        for s in client.get(
            f"/api/v1/schedule/weeks/{week['id']}/shifts", headers=admin_h
        ).json()
        if s["id"] == shift_id
    )
    assert row["start_time"] == "10:00" and row["end_time"] == "18:00"
    # approving again -> already processed (404)
    assert client.post(
        f"/api/v1/schedule/change-requests/{rid}/admin-approve", headers=admin_h
    ).status_code == 404
