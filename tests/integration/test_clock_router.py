"""Integration tests for the staff clock-in/out (QR session) router.

Full request path — auth (StaticIdentityProvider -> DB-RBAC / verified-identity) +
db (migrated sqlite) — with zero external credentials. Covers: session create ->
redeem -> a ``shifts`` row is written; single-use (double redeem rejected); TTL
(expired session rejected); used/completed rejected; self-only clocking (the written
shift is for the token-resolved employee); station page gating; and venue binding.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

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


def _station_h(**kw: Any) -> dict[str, str]:
    # The default seeded admin (uid admin-1, full_admin) bypasses the station page gate.
    return _auth(admin_token(**kw))


def _staff_h(email: str, uid: str = "staff") -> dict[str, str]:
    return _auth(non_admin_token(uid=uid, email=email))


def _seed_employee(client: TestClient, name: str, email: str) -> int:
    r = client.post(
        "/api/v1/employees", headers=_station_h(), json={"name": name, "email": email}
    )
    assert r.status_code in (200, 201), r.text
    return int(r.json()["id"])


def _exec(url: str, sql: str, params: dict[str, Any]) -> list[dict]:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(sql), params)
            if result.returns_rows:
                return [dict(r) for r in result.mappings().all()]
            return []
    finally:
        engine.dispose()


def _shifts_for(url: str, employee_id: int) -> list[dict]:
    return _exec(
        url,
        "SELECT id, employee_id, venue_id, shift_date, start_time, end_time, source "
        "FROM shifts WHERE employee_id = :eid ORDER BY id",
        {"eid": employee_id},
    )


# --- happy path: create -> redeem -> shift written -------------------------


def test_create_redeem_writes_shift(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    emp_id = _seed_employee(client, "Aya", "a@example.com")

    created = client.post("/api/v1/clock/sessions", headers=_station_h())
    assert created.status_code == 200, created.text
    body = created.json()
    token = body["token"]
    assert body["qr_payload"] == token
    assert body["expires_at"]

    redeemed = client.post(
        "/api/v1/clock/redeem",
        headers=_staff_h("a@example.com"),
        json={"token": token},
    )
    assert redeemed.status_code == 200, redeemed.text
    out = redeemed.json()
    assert out["status"] == "completed"
    assert out["kind"] == "clock_in"
    assert out["employee_name"] == "Aya"

    shifts = _shifts_for(url, emp_id)
    assert len(shifts) == 1
    assert shifts[0]["employee_id"] == emp_id
    assert shifts[0]["source"] == "clock"
    assert shifts[0]["end_time"] is None  # open shift

    # station poll now reflects the completed handoff (replaces onSnapshot)
    poll = client.get(f"/api/v1/clock/sessions/{token}", headers=_station_h())
    assert poll.status_code == 200
    assert poll.json()["status"] == "completed"
    assert poll.json()["kind"] == "clock_in"
    assert poll.json()["employee_name"] == "Aya"


def test_double_redeem_is_single_use(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    _seed_employee(client, "Aya", "a@example.com")
    token = client.post("/api/v1/clock/sessions", headers=_station_h()).json()["token"]

    first = client.post(
        "/api/v1/clock/redeem", headers=_staff_h("a@example.com"), json={"token": token}
    )
    assert first.status_code == 200
    second = client.post(
        "/api/v1/clock/redeem", headers=_staff_h("a@example.com"), json={"token": token}
    )
    assert second.status_code == 409  # already completed


def test_used_session_rejected(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    _seed_employee(client, "Aya", "a@example.com")
    token = client.post("/api/v1/clock/sessions", headers=_station_h()).json()["token"]
    # Simulate a claimed-but-not-completed session (the 'used' intermediate state).
    _exec(url, "UPDATE clock_sessions SET status = 'used' WHERE token = :t", {"t": token})

    r = client.post(
        "/api/v1/clock/redeem", headers=_staff_h("a@example.com"), json={"token": token}
    )
    assert r.status_code == 409


def test_expired_session_rejected(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    _seed_employee(client, "Aya", "a@example.com")
    token = client.post("/api/v1/clock/sessions", headers=_station_h()).json()["token"]
    # Force the TTL into the past.
    _exec(
        url,
        "UPDATE clock_sessions SET expires_at = :e WHERE token = :t",
        {"e": "2000-01-01T00:00:00+00:00", "t": token},
    )

    r = client.post(
        "/api/v1/clock/redeem", headers=_staff_h("a@example.com"), json={"token": token}
    )
    assert r.status_code == 410
    # lazy flip persisted -> the station poll shows 'expired'
    poll = client.get(f"/api/v1/clock/sessions/{token}", headers=_station_h())
    assert poll.json()["status"] == "expired"


def test_unknown_token_is_404(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    _seed_employee(client, "Aya", "a@example.com")
    r = client.post(
        "/api/v1/clock/redeem",
        headers=_staff_h("a@example.com"),
        json={"token": "does-not-exist"},
    )
    assert r.status_code == 404


# --- self-only clocking -----------------------------------------------------


def test_redeem_clocks_only_the_token_holder(app_client) -> None:  # type: ignore[no-untyped-def]
    """No body/param can name another employee: the shift lands on the token's owner."""
    _app, client, url = app_client
    a_id = _seed_employee(client, "Aya", "a@example.com")
    b_id = _seed_employee(client, "Ben", "b@example.com")
    token = client.post("/api/v1/clock/sessions", headers=_station_h()).json()["token"]

    # Ben's token redeems; even if a stray field tried to name Aya, it's ignored.
    r = client.post(
        "/api/v1/clock/redeem",
        headers=_staff_h("b@example.com", uid="ben"),
        json={"token": token, "employee_id": a_id, "email": "a@example.com"},
    )
    assert r.status_code == 200
    assert r.json()["employee_name"] == "Ben"
    assert _shifts_for(url, a_id) == []  # Aya untouched
    assert len(_shifts_for(url, b_id)) == 1


def test_redeem_unknown_employee_is_404(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    _seed_employee(client, "Aya", "a@example.com")
    token = client.post("/api/v1/clock/sessions", headers=_station_h()).json()["token"]
    # a verified token whose identity matches no employee
    r = client.post(
        "/api/v1/clock/redeem",
        headers=_staff_h("nobody@example.com", uid="nobody"),
        json={"token": token},
    )
    assert r.status_code == 404
    # session stays pending (not consumed by a failed identity)
    assert client.get(
        f"/api/v1/clock/sessions/{token}", headers=_station_h()
    ).json()["status"] == "pending"


# --- clock in -> clock out toggle ------------------------------------------


def test_second_session_clocks_out_open_shift(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    emp_id = _seed_employee(client, "Aya", "a@example.com")

    t1 = client.post("/api/v1/clock/sessions", headers=_station_h()).json()["token"]
    r1 = client.post(
        "/api/v1/clock/redeem", headers=_staff_h("a@example.com"), json={"token": t1}
    )
    assert r1.json()["kind"] == "clock_in"

    t2 = client.post("/api/v1/clock/sessions", headers=_station_h()).json()["token"]
    r2 = client.post(
        "/api/v1/clock/redeem", headers=_staff_h("a@example.com"), json={"token": t2}
    )
    assert r2.status_code == 200
    assert r2.json()["kind"] == "clock_out"

    shifts = _shifts_for(url, emp_id)
    assert len(shifts) == 1  # same shift closed, not a new one
    assert shifts[0]["end_time"] is not None


# --- station gating ---------------------------------------------------------


def test_create_requires_auth(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    assert client.post("/api/v1/clock/sessions").status_code == 401
    assert client.get("/api/v1/clock/sessions").status_code == 401


def test_non_admin_cannot_create_or_list(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    _seed_employee(client, "Aya", "a@example.com")
    h = _staff_h("a@example.com")
    assert client.post("/api/v1/clock/sessions", headers=h).status_code == 403
    assert client.get("/api/v1/clock/sessions", headers=h).status_code == 403


def test_page_gated_station_admin(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    seed_admin_user(
        url, admin_id=30, uid="sched", role="manager", allowed_pages=["schedule"]
    )
    seed_admin_user(
        url, admin_id=31, uid="stn", role="manager", allowed_pages=["station"]
    )
    sched_h = _auth(admin_token(uid="sched"))
    stn_h = _auth(admin_token(uid="stn"))
    # a schedule-only admin is denied the station page
    assert client.post("/api/v1/clock/sessions", headers=sched_h).status_code == 403
    assert client.get("/api/v1/clock/sessions", headers=sched_h).status_code == 403
    # a station admin is allowed
    assert client.post("/api/v1/clock/sessions", headers=stn_h).status_code == 200
    assert client.get("/api/v1/clock/sessions", headers=stn_h).status_code == 200


# --- venue binding ----------------------------------------------------------


def test_redeem_rejects_cross_venue(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    _seed_employee(client, "Aya", "a@example.com")  # venue 'default'
    # full_admin (bypasses the venue gate) mints a session in another venue
    created = client.post(
        "/api/v1/clock/sessions", headers=_station_h(venues=["venue-x"])
    )
    assert created.status_code == 200
    token = created.json()["token"]

    r = client.post(
        "/api/v1/clock/redeem", headers=_staff_h("a@example.com"), json={"token": token}
    )
    assert r.status_code == 403
    # rejected before the single-use claim -> still pending
    assert client.get(
        f"/api/v1/clock/sessions/{token}", headers=_station_h(venues=["venue-x"])
    ).json()["status"] == "pending"


# --- admin force-close ------------------------------------------------------


def test_force_close_session(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    _seed_employee(client, "Aya", "a@example.com")
    created = client.post("/api/v1/clock/sessions", headers=_station_h()).json()
    sid = created["id"]
    token = created["token"]

    closed = client.post(
        f"/api/v1/clock/sessions/{sid}/close", headers=_station_h()
    )
    assert closed.status_code == 200
    assert client.get(
        f"/api/v1/clock/sessions/{token}", headers=_station_h()
    ).json()["status"] == "expired"

    # a force-closed session can no longer be redeemed
    r = client.post(
        "/api/v1/clock/redeem", headers=_staff_h("a@example.com"), json={"token": token}
    )
    assert r.status_code == 410


def test_force_close_unknown_is_404(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    assert (
        client.post("/api/v1/clock/sessions/9999/close", headers=_station_h()).status_code
        == 404
    )
