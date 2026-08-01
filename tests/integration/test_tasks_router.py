"""Integration tests for the tasks & checklists router.

Full request path — auth (StaticIdentityProvider -> DB-RBAC / verified-identity),
db (migrated sqlite) — with zero external credentials. Focus: admin-vs-staff access,
manager-vs-full_admin (approval flow), ownership enforcement (staff A can't read or
act on staff B's run/task), clock-in gating, a full checklist-run lifecycle, and a
regression that query-param identity is no longer honoured.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from be.app.main import create_app
from be.db import get_session
from tests.conftest import admin_token, non_admin_token, seed_admin_user

TODAY = date.today().isoformat()


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


def _staff_h(email: str, uid: str) -> dict[str, str]:
    return _auth(non_admin_token(uid=uid, email=email))


def _seed_open_shift(url: str, *, employee_id: int, shift_date: str = TODAY) -> None:
    """Insert an open (``end_time IS NULL``) shift so the employee is 'clocked in'."""
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            nid = conn.execute(
                text("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM shifts")
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO shifts (id, venue_id, employee_id, role_id, shift_date, "
                    "start_time, end_time, source, created_at) VALUES "
                    "(:id, 'default', :eid, NULL, :d, '09:00', NULL, 'clock', :ts)"
                ),
                {"id": nid, "eid": employee_id, "d": shift_date, "ts": "2024-01-01T00:00:00+00:00"},
            )
    finally:
        engine.dispose()


def _seed_employees(client: TestClient) -> tuple[int, int]:
    h = _admin_h()
    a = client.post(
        "/api/v1/employees", headers=h, json={"name": "Aya", "email": "a@example.com"}
    ).json()
    b = client.post(
        "/api/v1/employees", headers=h, json={"name": "Ben", "email": "b@example.com"}
    ).json()
    return a["id"], b["id"]


def _build_checklist(
    client: TestClient, *, assignee_employee_ids: list[int], item_visible_to: list[int] | None = None
) -> int:
    """Admin: template + approved checklist + one mandatory item + assignees. Returns id."""
    h = _admin_h()
    tpl = client.post(
        "/api/v1/tasks/templates",
        headers=h,
        json={"title": "Mop the floor", "default_proof_type": "check"},
    ).json()
    cl = client.post(
        "/api/v1/tasks/checklists", headers=h, json={"name": "Closing", "category": "closing"}
    ).json()
    cid = cl["id"]
    item = {"task_template_id": tpl["id"], "is_mandatory": True}
    if item_visible_to is not None:
        item["assigned_employee_ids"] = item_visible_to
    client.put(
        f"/api/v1/tasks/checklists/{cid}/items",
        headers=h,
        json={"items": [item], "updated_at": cl["updated_at"]},
    )
    client.put(
        f"/api/v1/tasks/checklists/{cid}/assignees",
        headers=h,
        json={"assignees": [{"employee_id": e} for e in assignee_employee_ids]},
    )
    return cid


def _generate_run(client: TestClient, checklist_id: int) -> int:
    r = client.post(
        f"/api/v1/tasks/checklists/{checklist_id}/runs", headers=_admin_h(), json={"date": TODAY}
    )
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


# --- access control ---------------------------------------------------------


def test_no_token_is_401(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    assert client.get("/api/v1/tasks/templates").status_code == 401
    assert client.get("/api/v1/staff/checklist-runs").status_code == 401
    # query-param identity must NOT be accepted in lieu of a token
    assert client.get(
        "/api/v1/staff/checklist-runs", params={"email": "a@example.com"}
    ).status_code == 401


def test_non_admin_hitting_admin_route_is_403(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    _seed_employees(client)
    assert client.get(
        "/api/v1/tasks/templates", headers=_staff_h("a@example.com", "staff-a")
    ).status_code == 403


def test_manager_vs_full_admin_approval(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    seed_admin_user(url, admin_id=30, uid="mgr", role="manager", allowed_pages=["tasks"])
    mgr = _auth(admin_token(uid="mgr"))
    # manager can use the tasks page
    assert client.get("/api/v1/tasks/templates", headers=mgr).status_code == 200
    # a manager-created checklist lands in pending_approval (R36)
    cl = client.post("/api/v1/tasks/checklists", headers=mgr, json={"name": "Prep"}).json()
    assert cl["approval_status"] == "pending_approval"
    # manager cannot approve; full_admin can
    assert client.post(
        f"/api/v1/tasks/checklists/{cl['id']}/approve", headers=mgr
    ).status_code == 403
    ok = client.post(f"/api/v1/tasks/checklists/{cl['id']}/approve", headers=_admin_h())
    assert ok.status_code == 200 and ok.json()["approval_status"] == "approved"


def test_bonus_authoring_is_full_admin_only(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    seed_admin_user(url, admin_id=31, uid="mgr", role="manager", allowed_pages=["tasks"])
    mgr = _auth(admin_token(uid="mgr"))
    assert client.post(
        "/api/v1/tasks/templates", headers=mgr, json={"title": "Deep clean", "is_bonus": True}
    ).status_code == 403
    assert client.post(
        "/api/v1/tasks/templates", headers=_admin_h(), json={"title": "Deep clean", "is_bonus": True}
    ).status_code == 200


# --- ownership --------------------------------------------------------------


def test_query_param_identity_is_ignored(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    a_id, _b_id = _seed_employees(client)
    cid = _build_checklist(client, assignee_employee_ids=[a_id])
    _generate_run(client, cid)
    # B authenticates with their OWN token but tries to spoof A via ?email — the
    # verified token wins: B sees B's (empty) runs, not A's.
    b = _staff_h("b@example.com", "staff-b")
    resp = client.get("/api/v1/staff/checklist-runs", headers=b, params={"email": "a@example.com"})
    assert resp.status_code == 200
    assert resp.json()["runs"] == []


def test_staff_cannot_read_or_act_on_others_run(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    a_id, _b_id = _seed_employees(client)
    cid = _build_checklist(client, assignee_employee_ids=[a_id])
    run_id = _generate_run(client, cid)
    _seed_open_shift(url, employee_id=a_id)

    a = _staff_h("a@example.com", "staff-a")
    b = _staff_h("b@example.com", "staff-b")

    # A (assigned) reads the run + its task
    a_run = client.get(f"/api/v1/staff/checklist-runs/{run_id}", headers=a)
    assert a_run.status_code == 200
    run_task_id = a_run.json()["tasks"][0]["run_task_id"]

    # B is not an assignee -> 403 on read + on every mutating action
    assert client.get(f"/api/v1/staff/checklist-runs/{run_id}", headers=b).status_code == 403
    assert client.post(
        f"/api/v1/staff/checklist-runs/tasks/{run_task_id}/start", headers=b
    ).status_code == 403


def test_task_visibility_hides_from_other_assignee(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    a_id, b_id = _seed_employees(client)
    # both A and B are run assignees, but the task is visible ONLY to A
    cid = _build_checklist(
        client, assignee_employee_ids=[a_id, b_id], item_visible_to=[a_id]
    )
    run_id = _generate_run(client, cid)
    _seed_open_shift(url, employee_id=a_id)
    _seed_open_shift(url, employee_id=b_id)

    a = _staff_h("a@example.com", "staff-a")
    b = _staff_h("b@example.com", "staff-b")

    a_tasks = client.get(f"/api/v1/staff/checklist-runs/{run_id}", headers=a).json()["tasks"]
    assert len(a_tasks) == 1
    run_task_id = a_tasks[0]["run_task_id"]

    # B is an assignee (can read the run) but the task is invisible -> not listed,
    # and acting on it is indistinguishable from a missing task (404).
    b_run = client.get(f"/api/v1/staff/checklist-runs/{run_id}", headers=b)
    assert b_run.status_code == 200 and b_run.json()["tasks"] == []
    assert client.post(
        f"/api/v1/staff/checklist-runs/tasks/{run_task_id}/start", headers=b
    ).status_code == 404


# --- clock-in gating + lifecycle -------------------------------------------


def test_clock_in_gating_then_full_lifecycle(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    a_id, _b_id = _seed_employees(client)
    cid = _build_checklist(client, assignee_employee_ids=[a_id])
    run_id = _generate_run(client, cid)
    a = _staff_h("a@example.com", "staff-a")

    run_task_id = client.get(
        f"/api/v1/staff/checklist-runs/{run_id}", headers=a
    ).json()["tasks"][0]["run_task_id"]

    # not clocked in -> 403 not_clocked_in
    assert client.post(
        f"/api/v1/staff/checklist-runs/tasks/{run_task_id}/start", headers=a
    ).status_code == 403

    _seed_open_shift(url, employee_id=a_id)

    # start -> in_progress
    started = client.post(f"/api/v1/staff/checklist-runs/tasks/{run_task_id}/start", headers=a)
    assert started.status_code == 200
    assert started.json()["task"]["status"] == "in_progress"

    # finish -> done, and the run auto-completes (last mandatory task done)
    finished = client.post(
        f"/api/v1/staff/checklist-runs/tasks/{run_task_id}/finish-json", headers=a, json={}
    )
    assert finished.status_code == 200
    body = finished.json()
    assert body["task"]["status"] == "done"
    assert body["run_status"] == "completed"

    # admin runs board reflects completion
    board = client.get("/api/v1/tasks/runs", headers=_admin_h(), params={"date": TODAY}).json()
    row = next(r for r in board["runs"] if r["id"] == run_id)
    assert row["status"] == "completed"
    assert row["mandatory_done"] == row["mandatory_total"] == 1


def test_photo_proof_enforced_on_finish(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    a_id, _b_id = _seed_employees(client)
    h = _admin_h()
    tpl = client.post(
        "/api/v1/tasks/templates",
        headers=h,
        json={"title": "Photo the fridge", "default_proof_type": "photo"},
    ).json()
    cl = client.post("/api/v1/tasks/checklists", headers=h, json={"name": "Open"}).json()
    client.put(
        f"/api/v1/tasks/checklists/{cl['id']}/items",
        headers=h,
        json={"items": [{"task_template_id": tpl["id"], "is_mandatory": True}], "updated_at": cl["updated_at"]},
    )
    client.put(
        f"/api/v1/tasks/checklists/{cl['id']}/assignees",
        headers=h,
        json={"assignees": [{"employee_id": a_id}]},
    )
    run_id = _generate_run(client, cl["id"])
    _seed_open_shift(url, employee_id=a_id)
    a = _staff_h("a@example.com", "staff-a")
    run_task_id = client.get(
        f"/api/v1/staff/checklist-runs/{run_id}", headers=a
    ).json()["tasks"][0]["run_task_id"]

    client.post(f"/api/v1/staff/checklist-runs/tasks/{run_task_id}/start", headers=a)
    # no photo -> 400
    assert client.post(
        f"/api/v1/staff/checklist-runs/tasks/{run_task_id}/finish-json", headers=a, json={}
    ).status_code == 400
    # with a hosted proof url -> ok
    ok = client.post(
        f"/api/v1/staff/checklist-runs/tasks/{run_task_id}/finish-json",
        headers=a,
        json={"photos": ["tasks/proofs/x.jpg"]},
    )
    assert ok.status_code == 200 and ok.json()["task"]["status"] == "done"


# --- group audience resolution ---------------------------------------------


def test_group_assignee_resolution(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    a_id, b_id = _seed_employees(client)
    h = _admin_h()
    group = client.post("/api/v1/employee-groups", headers=h, json={"name": "Closers"}).json()
    client.put(
        f"/api/v1/employee-groups/{group['id']}/members", headers=h, json={"employee_ids": [a_id]}
    )
    tpl = client.post("/api/v1/tasks/templates", headers=h, json={"title": "Lock up"}).json()
    cl = client.post("/api/v1/tasks/checklists", headers=h, json={"name": "Nightly"}).json()
    client.put(
        f"/api/v1/tasks/checklists/{cl['id']}/items",
        headers=h,
        json={"items": [{"task_template_id": tpl["id"]}], "updated_at": cl["updated_at"]},
    )
    client.put(
        f"/api/v1/tasks/checklists/{cl['id']}/assignees",
        headers=h,
        json={"assignees": [{"group_id": group["id"]}]},
    )
    # resolve preview: only the group member (A) is in the audience
    prev = client.get(
        f"/api/v1/tasks/checklists/{cl['id']}/resolve-assignees", headers=h, params={"date": TODAY}
    ).json()
    ids = {e["id"] for e in prev["employees"]}
    assert ids == {a_id} and b_id not in ids


# --- admin reopen -----------------------------------------------------------


def test_admin_reopen_reverts_completed_run(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    a_id, _b_id = _seed_employees(client)
    cid = _build_checklist(client, assignee_employee_ids=[a_id])
    run_id = _generate_run(client, cid)
    _seed_open_shift(url, employee_id=a_id)
    a = _staff_h("a@example.com", "staff-a")
    run_task_id = client.get(
        f"/api/v1/staff/checklist-runs/{run_id}", headers=a
    ).json()["tasks"][0]["run_task_id"]
    client.post(f"/api/v1/staff/checklist-runs/tasks/{run_task_id}/start", headers=a)
    client.post(f"/api/v1/staff/checklist-runs/tasks/{run_task_id}/finish-json", headers=a, json={})

    reopened = client.post(
        f"/api/v1/tasks/runs/tasks/{run_task_id}/reopen", headers=_admin_h(), json={}
    )
    assert reopened.status_code == 200 and reopened.json()["reopened"] == 1
    detail = client.get(f"/api/v1/tasks/runs/{run_id}", headers=_admin_h()).json()
    assert detail["run"]["status"] == "open"
    assert detail["tasks"][0]["status"] == "in_progress"
