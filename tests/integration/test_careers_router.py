"""Integration tests for the careers router.

Full request path — auth (StaticIdentityProvider -> DB-RBAC), db (migrated sqlite),
fake Storage + a fake Notifier — zero external credentials.

Focus:
* PUBLIC apply works UNAUTHENTICATED (no token) and is write-only;
* the PUBLIC positions list returns only ACTIVE positions and leaks NO applicant PII;
* admin review + CV download require the ``careers`` page (401 no token / 403 non-admin
  or admin lacking the page / 200 with the page or full_admin);
* application detail + CV bytes are admin-only (the public surface has no route to them);
* CV upload validates MIME + one-shot; status update validated against the enum;
* the public endpoints are rate-limited (in-process guard).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from be.adapters.notify.noop import NoopNotifier
from be.adapters.storage.local import LocalStorage
from be.app.deps import get_notify, get_storage
from be.app.domains.careers.router import get_rate_limiter
from be.app.domains.careers.service import FixedWindowRateLimiter
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

    shared_storage = LocalStorage()
    shared_notifier = NoopNotifier()
    # Generous limiter so multi-request tests don't trip it; a dedicated test overrides.
    limiter = FixedWindowRateLimiter(max_requests=1000, window_seconds=60.0)
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_storage] = lambda: shared_storage
    app.dependency_overrides[get_notify] = lambda: shared_notifier
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    client = TestClient(app)
    try:
        yield app, client, migrated_db, shared_notifier
    finally:
        app.dependency_overrides.clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _admin_h() -> dict[str, str]:
    return _auth(admin_token())


def _good_application(**over: Any) -> dict:
    body = {
        "full_name": "Aya Cohen",
        "email": "aya@example.com",
        "phone": "+972 50 123 4567",
        "city": "Tel Aviv",
        "street": "Dizengoff 1",
        "experience": "I have worked in restaurants for several years now.",
        "start_date": "2026-09-01",
        "citizenship": True,
        "english": True,
        "lang": "en",
    }
    body.update(over)
    return body


def _create_position(client: TestClient, **body: Any) -> dict:
    r = client.post("/api/v1/admin/careers/positions", headers=_admin_h(), json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --- public apply (unauthenticated) -----------------------------------------


def test_public_apply_works_unauthenticated(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url, notifier = app_client
    pos = _create_position(client, title_en="Chef", title_he="שף", is_active=True)

    # NO Authorization header at all
    r = client.post(
        "/api/v1/careers/applications",
        json=_good_application(position_id=pos["id"]),
    )
    assert r.status_code == 200, r.text
    # write-only: echoes just the id, no PII
    assert set(r.json().keys()) == {"id"}

    # notifier seam fired, carrying no sensitive PII beyond name/position
    kinds = [k for (k, _p) in notifier.sent]
    assert "careers_application_new" in kinds
    payload = next(p for (k, p) in notifier.sent if k == "careers_application_new")
    assert payload["applicant_name"] == "Aya Cohen"
    assert "email" not in payload and "phone" not in payload


def test_public_apply_validates_body(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url, _n = app_client
    bad = client.post("/api/v1/careers/applications", json=_good_application(email="nope"))
    assert bad.status_code == 400
    short = client.post(
        "/api/v1/careers/applications", json=_good_application(experience="too short")
    )
    assert short.status_code == 400


def test_public_apply_empty_post_is_400_not_draft(app_client) -> None:  # type: ignore[no-untyped-def]
    """Public apply is NOT a draft endpoint — an empty body fails validation."""
    _app, client, _url, _n = app_client
    assert client.post("/api/v1/careers/applications", json={}).status_code == 400


# --- public positions list (no PII, active-only) ----------------------------


def test_public_positions_active_only_and_no_pii(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url, _n = app_client
    active = _create_position(client, title_en="Waiter", is_active=True)
    _create_position(client, title_en="Hidden role", is_active=False)

    # apply so an application (PII) exists in the db
    client.post(
        "/api/v1/careers/applications",
        json=_good_application(position_id=active["id"]),
    )

    r = client.get("/api/v1/careers/positions")  # unauthenticated
    assert r.status_code == 200
    rows = r.json()
    ids = [p["id"] for p in rows]
    assert active["id"] in ids
    # inactive positions are absent
    assert all(p["title_en"] != "Hidden role" for p in rows)
    # no applicant PII fields leak into the public projection
    blob = r.text.lower()
    for pii in ("aya", "example.com", "972 50", "dizengoff", "email", "phone"):
        assert pii not in blob


# --- admin gating -----------------------------------------------------------


def test_admin_endpoints_require_the_careers_page(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url, _n = app_client

    # no token -> 401
    assert client.get("/api/v1/admin/careers/positions").status_code == 401
    assert client.get("/api/v1/admin/careers/applications").status_code == 401

    # a non-admin (no admin_users row) -> 403
    non_admin = _auth(non_admin_token(uid="u-x", email="x@example.com"))
    assert client.get("/api/v1/admin/careers/applications", headers=non_admin).status_code == 403

    # an admin WITHOUT the careers page -> 403
    seed_admin_user(
        url, admin_id=2, uid="mgr-1", email="mgr@example.test", role="manager",
        allowed_pages=["menu"],
    )
    mgr_h = _auth(admin_token(uid="mgr-1", email="mgr@example.test"))
    assert client.get("/api/v1/admin/careers/applications", headers=mgr_h).status_code == 403

    # an admin WITH the careers page -> 200
    seed_admin_user(
        url, admin_id=3, uid="car-1", email="car@example.test", role="manager",
        allowed_pages=["careers"],
    )
    car_h = _auth(admin_token(uid="car-1", email="car@example.test"))
    assert client.get("/api/v1/admin/careers/applications", headers=car_h).status_code == 200

    # the default full_admin bypasses the page gate -> 200
    assert client.get("/api/v1/admin/careers/applications", headers=_admin_h()).status_code == 200


def test_application_detail_is_admin_only(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url, _n = app_client
    pos = _create_position(client, title_en="Chef")
    aid = client.post(
        "/api/v1/careers/applications", json=_good_application(position_id=pos["id"])
    ).json()["id"]

    # there is NO public route to read an application back
    assert client.get(f"/api/v1/careers/applications/{aid}").status_code == 404

    # admin sees full PII
    r = client.get(f"/api/v1/admin/careers/applications/{aid}", headers=_admin_h())
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "aya@example.com" and body["phone"].startswith("+972")


# --- position management -----------------------------------------------------


def test_position_crud_and_empty_post_creates_draft(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url, _n = app_client
    # house convention: POST {} makes a draft position
    draft = _create_position(client)
    assert draft["department"] == "service" and draft["is_active"] is True

    # update titles + responsibilities (stored JSON-as-TEXT, parsed back to a list)
    upd = client.put(
        f"/api/v1/admin/careers/positions/{draft['id']}",
        headers=_admin_h(),
        json={"title_en": "Sous Chef", "responsibilities_en": ["prep", "line"]},
    )
    assert upd.status_code == 200
    assert upd.json()["title_en"] == "Sous Chef"
    assert upd.json()["responsibilities_en"] == ["prep", "line"]

    # toggle inactive -> drops out of the public list
    client.post(
        f"/api/v1/admin/careers/positions/{draft['id']}/active",
        headers=_admin_h(),
        json={"is_active": False},
    )
    pub_ids = [p["id"] for p in client.get("/api/v1/careers/positions").json()]
    assert draft["id"] not in pub_ids

    # delete
    assert client.delete(
        f"/api/v1/admin/careers/positions/{draft['id']}", headers=_admin_h()
    ).status_code == 200
    assert client.get(
        f"/api/v1/admin/careers/positions/{draft['id']}", headers=_admin_h()
    ).status_code == 404


def test_status_update_validated(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url, _n = app_client
    pos = _create_position(client)
    aid = client.post(
        "/api/v1/careers/applications", json=_good_application(position_id=pos["id"])
    ).json()["id"]

    bad = client.put(
        f"/api/v1/admin/careers/applications/{aid}/status",
        headers=_admin_h(),
        json={"status": "banana"},
    )
    assert bad.status_code == 400

    ok = client.put(
        f"/api/v1/admin/careers/applications/{aid}/status",
        headers=_admin_h(),
        json={"status": "reviewed"},
    )
    assert ok.status_code == 200 and ok.json()["status"] == "reviewed"

    # status filter on the list
    rows = client.get(
        "/api/v1/admin/careers/applications?status=reviewed", headers=_admin_h()
    ).json()
    assert [a["id"] for a in rows] == [aid]


# --- CV upload + admin-only download ----------------------------------------


def test_cv_upload_validation_and_admin_only_download(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url, _n = app_client
    pos = _create_position(client)
    aid = client.post(
        "/api/v1/careers/applications", json=_good_application(position_id=pos["id"])
    ).json()["id"]

    # wrong MIME rejected
    bad = client.post(
        f"/api/v1/careers/applications/{aid}/cv",
        files={"cv": ("shot.png", b"x", "image/png")},
    )
    assert bad.status_code == 400

    # valid PDF upload (public, unauthenticated)
    pdf = b"%PDF-1.4 fake resume bytes"
    up = client.post(
        f"/api/v1/careers/applications/{aid}/cv",
        files={"cv": ("resume.pdf", pdf, "application/pdf")},
    )
    assert up.status_code == 200, up.text

    # one-shot: a second upload is rejected (no hijacking another applicant's CV)
    second = client.post(
        f"/api/v1/careers/applications/{aid}/cv",
        files={"cv": ("resume2.pdf", pdf, "application/pdf")},
    )
    assert second.status_code == 409

    # there is NO public route to download the CV (POST-only path -> 405, never 200)
    assert client.get(f"/api/v1/careers/applications/{aid}/cv").status_code in (404, 405)

    # admin proxy-streams the exact bytes with an attachment disposition
    got = client.get(
        f"/api/v1/admin/careers/applications/{aid}/cv", headers=_admin_h()
    )
    assert got.status_code == 200 and got.content == pdf
    assert "attachment" in got.headers["content-disposition"]


# --- rate limiting ----------------------------------------------------------


def test_public_apply_rate_limited(app_client) -> None:  # type: ignore[no-untyped-def]
    app, client, _url, _n = app_client
    # swap in a tight limiter (1 request / window)
    tight = FixedWindowRateLimiter(max_requests=1, window_seconds=60.0)
    app.dependency_overrides[get_rate_limiter] = lambda: tight

    first = client.post("/api/v1/careers/applications", json=_good_application())
    assert first.status_code == 200
    second = client.post("/api/v1/careers/applications", json=_good_application())
    assert second.status_code == 429
