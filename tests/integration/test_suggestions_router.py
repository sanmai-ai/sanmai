"""Integration tests for the staff suggestions router.

Full request path — auth (StaticIdentityProvider -> DB-RBAC / verified-identity),
db (migrated sqlite), fake Storage + a fake Notifier — zero external credentials.

Focus:
* admin moderation/settings gated by require_admin("suggestions") — 401 without a
  token, 403 for a non-admin and for an admin lacking the page (the regression for
  the live email-spoofable bespoke admin check);
* staff-self identity resolved from the VERIFIED token — a hidden suggestion is
  author-only, and only the author may attach images (the no-identity / query-param
  impersonation hole is closed);
* plus-one idempotency, image upload via the Storage seam + proxy retrieval, and the
  best-effort new-suggestion Notifier send.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from be.adapters.notify.noop import NoopNotifier
from be.adapters.storage.local import LocalStorage
from be.app.deps import get_notify, get_storage
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

    shared_storage = LocalStorage()  # one instance so bytes persist across requests
    shared_notifier = NoopNotifier()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_storage] = lambda: shared_storage
    app.dependency_overrides[get_notify] = lambda: shared_notifier
    client = TestClient(app)
    try:
        yield app, client, migrated_db, shared_notifier
    finally:
        app.dependency_overrides.clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _admin_h() -> dict[str, str]:
    return _auth(admin_token())


def _staff_h(email: str, uid: str) -> dict[str, str]:
    return _auth(non_admin_token(uid=uid, email=email))


def _seed_employees(client: TestClient) -> tuple[int, int]:
    h = _admin_h()
    a = client.post(
        "/api/v1/employees", headers=h, json={"name": "Aya", "email": "a@example.com"}
    ).json()
    b = client.post(
        "/api/v1/employees", headers=h, json={"name": "Ben", "email": "b@example.com"}
    ).json()
    return a["id"], b["id"]


def _create_suggestion(client: TestClient, h: dict[str, str], **body: Any) -> dict:
    r = client.post("/api/v1/staff/suggestions", headers=h, json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --- access control ---------------------------------------------------------


def test_no_token_is_401(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url, _n = app_client
    assert client.get("/api/v1/staff/suggestions").status_code == 401
    assert client.get("/api/v1/admin/suggestions").status_code == 401
    assert client.get("/api/v1/admin/suggestions-settings").status_code == 401


def test_admin_endpoints_require_the_suggestions_page(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url, _n = app_client
    _seed_employees(client)

    # a non-admin (no admin_users row) -> 403
    assert (
        client.get("/api/v1/admin/suggestions", headers=_staff_h("a@example.com", "u-a")).status_code
        == 403
    )

    # an admin WITHOUT the suggestions page -> 403
    seed_admin_user(
        url, admin_id=2, uid="mgr-1", email="mgr@example.test", role="manager",
        allowed_pages=["menu"],
    )
    mgr_h = _auth(admin_token(uid="mgr-1", email="mgr@example.test"))
    assert client.get("/api/v1/admin/suggestions", headers=mgr_h).status_code == 403

    # an admin WITH the suggestions page -> 200
    seed_admin_user(
        url, admin_id=3, uid="sug-1", email="sug@example.test", role="manager",
        allowed_pages=["suggestions"],
    )
    sug_h = _auth(admin_token(uid="sug-1", email="sug@example.test"))
    assert client.get("/api/v1/admin/suggestions", headers=sug_h).status_code == 200

    # the default full_admin bypasses the page gate -> 200
    assert client.get("/api/v1/admin/suggestions", headers=_admin_h()).status_code == 200


# --- create + notify --------------------------------------------------------


def test_create_lists_and_notifies(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url, notifier = app_client
    _seed_employees(client)
    a = _staff_h("a@example.com", "u-a")

    created = _create_suggestion(
        client, a, title="Buy a new mop", category="kitchen", description="the old one is gone"
    )
    sid = created["id"]

    # best-effort Notifier seam recorded the new-suggestion event
    kinds = [k for (k, _payload) in notifier.sent]
    assert "suggestion_new" in kinds
    payload = next(p for (k, p) in notifier.sent if k == "suggestion_new")
    assert payload["suggestion_id"] == sid and payload["author_name"] == "Aya"

    rows = client.get("/api/v1/staff/suggestions", headers=a).json()
    assert [r["id"] for r in rows] == [sid]
    assert rows[0]["is_own"] is True and rows[0]["author"] == "Aya"


def test_empty_post_creates_a_draft(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url, _n = app_client
    _seed_employees(client)
    a = _staff_h("a@example.com", "u-a")
    created = _create_suggestion(client, a)  # POST {}
    detail = client.get(f"/api/v1/staff/suggestions/{created['id']}", headers=a).json()
    assert detail["title"] == "Untitled suggestion" and detail["category"] == "other"


# --- authorship / visibility (hidden -> author-only) ------------------------


def test_hidden_suggestion_is_author_only(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url, _n = app_client
    _seed_employees(client)
    a = _staff_h("a@example.com", "u-a")
    b = _staff_h("b@example.com", "u-b")
    sid = _create_suggestion(client, a, title="Idea", description="d")["id"]

    # admin hides it
    r = client.put(
        f"/api/v1/admin/suggestions/{sid}", headers=_admin_h(), json={"is_hidden": True}
    )
    assert r.status_code == 200, r.text

    # author still sees it; another staff member gets 404 and it's absent from their list
    assert client.get(f"/api/v1/staff/suggestions/{sid}", headers=a).status_code == 200
    assert client.get(f"/api/v1/staff/suggestions/{sid}", headers=b).status_code == 404
    assert client.get("/api/v1/staff/suggestions", headers=b).json() == []

    # admin sees hidden ones
    admin_rows = client.get("/api/v1/admin/suggestions", headers=_admin_h()).json()
    assert any(x["id"] == sid and x["is_hidden"] for x in admin_rows)


def test_query_param_identity_is_ignored(app_client) -> None:  # type: ignore[no-untyped-def]
    """Regression: a spoofed ?email=/?phone= must NOT override the token identity."""
    _app, client, _url, _n = app_client
    _seed_employees(client)
    a = _staff_h("a@example.com", "u-a")
    sid = _create_suggestion(client, a, title="Mine", description="d")["id"]

    # a's token but b's email in the query string -> still resolves as a (is_own True)
    detail = client.get(
        f"/api/v1/staff/suggestions/{sid}?email=b@example.com&phone=+972500000000",
        headers=a,
    ).json()
    assert detail["is_own"] is True


# --- plus-one idempotency ---------------------------------------------------


def test_plus_one_is_idempotent(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url, _n = app_client
    _seed_employees(client)
    a = _staff_h("a@example.com", "u-a")
    b = _staff_h("b@example.com", "u-b")
    sid = _create_suggestion(client, a, title="Idea", description="d")["id"]

    first = client.post(f"/api/v1/staff/suggestions/{sid}/plus-one", headers=b).json()
    second = client.post(f"/api/v1/staff/suggestions/{sid}/plus-one", headers=b).json()
    assert first == {"plus_one_count": 1, "viewer_plus_oned": True}
    assert second == {"plus_one_count": 1, "viewer_plus_oned": True}  # no double-count

    removed = client.delete(f"/api/v1/staff/suggestions/{sid}/plus-one", headers=b).json()
    assert removed == {"plus_one_count": 0, "viewer_plus_oned": False}


# --- comments ---------------------------------------------------------------


def test_comment_blocked_on_closed(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url, _n = app_client
    _seed_employees(client)
    a = _staff_h("a@example.com", "u-a")
    b = _staff_h("b@example.com", "u-b")
    sid = _create_suggestion(client, a, title="Idea", description="d")["id"]

    ok = client.post(
        f"/api/v1/staff/suggestions/{sid}/comments", headers=b, json={"body": "nice"}
    )
    assert ok.status_code == 200, ok.text

    # empty body -> 400
    assert (
        client.post(
            f"/api/v1/staff/suggestions/{sid}/comments", headers=b, json={"body": "  "}
        ).status_code
        == 400
    )

    # admin closes -> no more comments
    client.put(f"/api/v1/admin/suggestions/{sid}", headers=_admin_h(), json={"status": "closed"})
    closed = client.post(
        f"/api/v1/staff/suggestions/{sid}/comments", headers=b, json={"body": "late"}
    )
    assert closed.status_code == 400

    detail = client.get(f"/api/v1/staff/suggestions/{sid}", headers=a).json()
    assert len(detail["comments"]) == 1 and detail["comments"][0]["body"] == "nice"


# --- image upload via the Storage seam --------------------------------------


def test_image_upload_and_proxy(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url, _n = app_client
    _seed_employees(client)
    a = _staff_h("a@example.com", "u-a")
    b = _staff_h("b@example.com", "u-b")
    sid = _create_suggestion(client, a, title="Idea", description="d")["id"]

    png = b"\x89PNG\r\n\x1a\n fake bytes"
    files = {"images": ("shot.png", png, "image/png")}

    # non-author cannot attach images
    assert (
        client.post(f"/api/v1/staff/suggestions/{sid}/images", headers=b, files=files).status_code
        == 403
    )

    # author uploads -> key under suggestions/{id}/
    up = client.post(f"/api/v1/staff/suggestions/{sid}/images", headers=a, files=files)
    assert up.status_code == 200, up.text
    key = up.json()["images"][0]
    assert key.startswith(f"suggestions/{sid}/")

    # the key surfaces on detail + proxy-streams the exact bytes back
    detail = client.get(f"/api/v1/staff/suggestions/{sid}", headers=a).json()
    assert detail["images"][0]["url"] == key
    got = client.get(f"/api/v1/suggestions/image?path={key}", headers=a)
    assert got.status_code == 200 and got.content == png

    # a path outside the suggestions/ prefix is rejected
    assert client.get("/api/v1/suggestions/image?path=tasks/secret.png", headers=a).status_code == 400


# --- settings ---------------------------------------------------------------


def test_settings_get_and_update(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url, _n = app_client
    _seed_employees(client)

    # lazily-seeded default is the neutral empty string (no San-Mai default)
    s = client.get("/api/v1/admin/suggestions-settings", headers=_admin_h()).json()
    assert s["notification_email"] == ""

    bad = client.put(
        "/api/v1/admin/suggestions-settings", headers=_admin_h(),
        json={"notification_email": "nope"},
    )
    assert bad.status_code == 400

    ok = client.put(
        "/api/v1/admin/suggestions-settings", headers=_admin_h(),
        json={"notification_email": "ops@example.test"},
    )
    assert ok.status_code == 200 and ok.json()["notification_email"] == "ops@example.test"
