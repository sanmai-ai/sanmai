"""Integration tests for the forms / courses / flows / assignments router.

Full request path — auth (StaticIdentityProvider -> DB-RBAC / verified-identity),
db (migrated sqlite), fake Storage + a fake PdfRenderer — zero external credentials.

Focus:
* admin authoring gated by require_admin("forms");
* the signed-document / PII endpoints REQUIRE require_admin("hr") — 401 without a token
  and 403 for a forms-only admin (the regression test for the live UNAUTHENTICATED
  signed-ID/bank-doc leak);
* staff-self identity is resolved from the VERIFIED token — a staff member cannot read
  or write another employee's response / draft / PDF / assignment / course-progress,
  and the no-identity save-draft hole is closed;
* the empty-draft POST {} convention, assignment fan-out, sign -> PDF via the seam,
  and flow sequential unlock.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from be.adapters.storage.local import LocalStorage
from be.app.deps import get_storage
from be.app.domains.forms.render import get_pdf_renderer
from be.app.main import create_app
from be.db import get_session
from tests.conftest import admin_token, non_admin_token, seed_admin_user


class _FakePdfRenderer:
    """Deterministic renderer so the sign/stream path is exercised without WeasyPrint."""

    def render(self, template: dict, response: dict, employee: dict) -> tuple[bytes, str]:
        return b"%PDF-1.4 fake", "cafebabe" * 8


@pytest.fixture
def app_client(sessionmaker_for, migrated_db):  # type: ignore[no-untyped-def]
    app = create_app()
    sm = sessionmaker_for

    async def _override_session() -> Any:
        async with sm() as session:
            yield session

    shared_storage = LocalStorage()  # one instance so bytes persist across requests
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_pdf_renderer] = lambda: _FakePdfRenderer()
    app.dependency_overrides[get_storage] = lambda: shared_storage
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


def _seed_employees(client: TestClient) -> tuple[int, int]:
    h = _admin_h()
    a = client.post(
        "/api/v1/employees", headers=h, json={"name": "Aya", "email": "a@example.com"}
    ).json()
    b = client.post(
        "/api/v1/employees", headers=h, json={"name": "Ben", "email": "b@example.com"}
    ).json()
    return a["id"], b["id"]


def _make_published_template(client: TestClient, *, signature: bool = False) -> str:
    h = _admin_h()
    fields = [{"type": "text", "name": "full_name"}]
    if signature:
        fields.append({"type": "signature", "name": "sig"})
    tpl = client.post(
        "/api/v1/admin/forms/templates", headers=h, json={"name_en": "NDA", "fields": fields}
    ).json()
    client.post(f"/api/v1/admin/forms/templates/{tpl['id']}/publish", headers=h)
    return tpl["id"]


# --- access control ---------------------------------------------------------


def test_no_token_is_401(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    assert client.get("/api/v1/admin/forms/templates").status_code == 401
    assert client.get("/api/v1/staff/assignments").status_code == 401
    # query-param identity must NOT be accepted in lieu of a token
    assert client.get(
        "/api/v1/staff/assignments", params={"email": "a@example.com"}
    ).status_code == 401


def test_forms_admin_authoring_gate(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    _seed_employees(client)
    # a staff (non-admin) token is 403 on authoring
    assert client.get(
        "/api/v1/admin/forms/templates", headers=_staff_h("a@example.com", "staff-a")
    ).status_code == 403
    # an admin holding an unrelated page (not forms) is 403
    seed_admin_user(url, admin_id=50, uid="menu-admin", role="manager", allowed_pages=["menu"])
    assert client.get(
        "/api/v1/admin/forms/templates", headers=_auth(admin_token(uid="menu-admin"))
    ).status_code == 403
    # a forms admin is allowed
    seed_admin_user(url, admin_id=51, uid="forms-admin", role="manager", allowed_pages=["forms"])
    assert client.get(
        "/api/v1/admin/forms/templates", headers=_auth(admin_token(uid="forms-admin"))
    ).status_code == 200


# --- PII / signed-document gate (regression for the live leak) --------------


def test_pii_endpoints_require_hr_admin(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    tpl_id = _make_published_template(client)
    # forms-only admin can author but is a stand-in for "not HR"
    seed_admin_user(url, admin_id=60, uid="forms-only", role="manager", allowed_pages=["forms"])
    forms_only = _auth(admin_token(uid="forms-only"))
    fake_resp = "00000000-0000-0000-0000-000000000000"

    pii_paths = [
        f"/api/v1/admin/forms/templates/{tpl_id}/responses",
        f"/api/v1/admin/forms/responses/{fake_resp}/pdf-url",
        f"/api/v1/admin/forms/responses/{fake_resp}/pdf",
    ]
    for path in pii_paths:
        # no token -> 401 (was fully unauthenticated in the live system)
        assert client.get(path).status_code == 401, path
        # authenticated but not HR -> 403
        assert client.get(path, headers=forms_only).status_code == 403, path

    # a full_admin (has hr) reaches the endpoints (200, or 404 for the fake id)
    assert client.get(pii_paths[0], headers=_admin_h()).status_code == 200
    assert client.get(pii_paths[1], headers=_admin_h()).status_code == 200
    assert client.get(pii_paths[2], headers=_admin_h()).status_code == 404


def test_hr_only_admin_can_read_pii_but_not_author(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client
    tpl_id = _make_published_template(client)
    seed_admin_user(url, admin_id=61, uid="hr-only", role="manager", allowed_pages=["hr"])
    hr_only = _auth(admin_token(uid="hr-only"))
    # HR admin reads responses (PII) ...
    assert client.get(
        f"/api/v1/admin/forms/templates/{tpl_id}/responses", headers=hr_only
    ).status_code == 200
    # ... but cannot author templates (that is the forms page)
    assert client.post(
        "/api/v1/admin/forms/templates", headers=hr_only, json={}
    ).status_code == 403


# --- empty-draft convention -------------------------------------------------


def test_post_empty_body_creates_drafts(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    h = _admin_h()
    assert client.post("/api/v1/admin/forms/templates", headers=h, json={}).status_code == 200
    assert client.post("/api/v1/admin/forms/courses", headers=h, json={}).status_code == 200
    assert client.post("/api/v1/admin/forms/flows", headers=h, json={}).status_code == 200


# --- template versioning ----------------------------------------------------


def test_publish_then_new_draft_bumps_version(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    h = _admin_h()
    tpl_id = _make_published_template(client)
    # reopen for editing -> new draft in the same chain
    draft_id = client.post(
        f"/api/v1/admin/forms/templates/{tpl_id}/new-draft", headers=h
    ).json()["id"]
    assert draft_id != tpl_id
    # publishing the draft creates v2 and archives the old published + the draft
    published = client.post(
        f"/api/v1/admin/forms/templates/{draft_id}/publish", headers=h
    ).json()
    assert published["version"] == 2
    old = client.get(f"/api/v1/admin/forms/templates/{tpl_id}", headers=h).json()
    assert old["status"] == "archived"


# --- staff-self: fill + ownership ------------------------------------------


def test_staff_fill_save_and_ownership(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    _seed_employees(client)
    tpl_id = _make_published_template(client)
    a = _staff_h("a@example.com", "staff-a")
    b = _staff_h("b@example.com", "staff-b")

    # A starts + saves a draft
    resp_id = client.post(
        "/api/v1/staff/form-responses", headers=a, json={"template_id": tpl_id}
    ).json()["id"]
    saved = client.patch(
        f"/api/v1/staff/form-responses/{resp_id}", headers=a, json={"answers": {"full_name": "Aya"}}
    )
    assert saved.status_code == 200
    assert saved.json()["answers"] == {"full_name": "Aya"}

    # B cannot save into A's draft (this closes the no-identity save-draft hole) -> 403
    assert client.patch(
        f"/api/v1/staff/form-responses/{resp_id}", headers=b, json={"answers": {"full_name": "hax"}}
    ).status_code == 403
    # B cannot read A's draft via the resume endpoint -> 403
    assert client.get(
        f"/api/v1/staff/forms/{tpl_id}", headers=b, params={"response_id": resp_id}
    ).status_code == 403
    # A resumes their own draft
    resumed = client.get(f"/api/v1/staff/forms/{tpl_id}", headers=a).json()
    assert resumed["draft"]["response_id"] == resp_id


def test_staff_submit_no_signature_and_sign_pdf(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    _seed_employees(client)
    a = _staff_h("a@example.com", "staff-a")

    # no-signature template -> /submit path (400 if it has a signature field)
    plain_id = _make_published_template(client, signature=False)
    r1 = client.post("/api/v1/staff/form-responses", headers=a, json={"template_id": plain_id}).json()
    ok = client.post(
        f"/api/v1/staff/form-responses/{r1['id']}/submit", headers=a, json={"answers": {"x": 1}}
    )
    assert ok.status_code == 200 and ok.json()["success"] is True

    # a signature template rejects /submit ...
    sig_id = _make_published_template(client, signature=True)
    r2 = client.post("/api/v1/staff/form-responses", headers=a, json={"template_id": sig_id}).json()
    assert client.post(
        f"/api/v1/staff/form-responses/{r2['id']}/submit", headers=a, json={"answers": {}}
    ).status_code == 400
    # ... and /sign renders a PDF via the fake renderer
    signed = client.post(
        f"/api/v1/staff/form-responses/{r2['id']}/sign",
        headers=a,
        json={"signature_base64": "AAAA", "answers": {"full_name": "Aya"}},
    )
    assert signed.status_code == 200
    assert signed.json()["pdf_sha256"] == "cafebabe" * 8

    # owner streams their signed PDF; another employee cannot
    b = _staff_h("b@example.com", "staff-b")
    assert client.get(f"/api/v1/staff/form-responses/{r2['id']}/pdf", headers=a).status_code == 200
    assert client.get(f"/api/v1/staff/form-responses/{r2['id']}/pdf", headers=b).status_code == 403
    # an HR admin can stream it too (PII access)
    admin_pdf = client.get(f"/api/v1/admin/forms/responses/{r2['id']}/pdf", headers=_admin_h())
    assert admin_pdf.status_code == 200 and admin_pdf.content == b"%PDF-1.4 fake"


def test_signed_docs_scoped_to_caller(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    _seed_employees(client)
    tpl_id = _make_published_template(client, signature=True)
    a = _staff_h("a@example.com", "staff-a")
    b = _staff_h("b@example.com", "staff-b")
    r = client.post("/api/v1/staff/form-responses", headers=a, json={"template_id": tpl_id}).json()
    client.post(
        f"/api/v1/staff/form-responses/{r['id']}/sign",
        headers=a,
        json={"signature_base64": "AAAA", "answers": {}},
    )
    assert len(client.get("/api/v1/staff/signed-docs", headers=a).json()) == 1
    assert client.get("/api/v1/staff/signed-docs", headers=b).json() == []


# --- assignments: fan-out, ownership, flow sequential unlock ----------------


def test_assignment_fanout_and_ownership(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    a_id, b_id = _seed_employees(client)
    tpl_id = _make_published_template(client)
    h = _admin_h()
    created = client.post(
        "/api/v1/admin/forms/assignments",
        headers=h,
        json={"employee_id": [a_id, b_id], "source": "standalone", "form_id": tpl_id},
    ).json()
    assert created["count"] == 2

    # each staff sees only their own assignment
    a = _staff_h("a@example.com", "staff-a")
    b = _staff_h("b@example.com", "staff-b")
    a_list = client.get("/api/v1/staff/assignments", headers=a).json()
    b_list = client.get("/api/v1/staff/assignments", headers=b).json()
    assert len(a_list) == 1 and len(b_list) == 1
    a_aid = a_list[0]["id"]
    # B cannot drill into A's assignment
    assert client.get(f"/api/v1/staff/assignments/{a_aid}", headers=b).status_code == 403
    assert client.get(f"/api/v1/staff/assignments/{a_aid}", headers=a).status_code == 200

    # invalid source (both form + course) -> 400
    bad = client.post(
        "/api/v1/admin/forms/assignments",
        headers=h,
        json={"employee_id": a_id, "source": "standalone", "form_id": tpl_id, "course_id": tpl_id},
    )
    assert bad.status_code == 400


def test_flow_assignment_sequential_unlock(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    a_id, _b = _seed_employees(client)
    h = _admin_h()
    t1 = _make_published_template(client, signature=True)
    t2 = _make_published_template(client, signature=True)
    flow = client.post(
        "/api/v1/admin/forms/flows", headers=h, json={"name_en": "Onboarding", "ordering": "sequential"}
    ).json()
    fid = flow["id"]
    client.post(
        f"/api/v1/admin/forms/flows/{fid}/items",
        headers=h,
        json={"position": 0, "item_type": "form", "item_id": t1},
    )
    client.post(
        f"/api/v1/admin/forms/flows/{fid}/items",
        headers=h,
        json={"position": 1, "item_type": "form", "item_id": t2},
    )
    client.post(f"/api/v1/admin/forms/flows/{fid}/publish", headers=h)

    client.post(
        "/api/v1/admin/forms/assignments",
        headers=h,
        json={"employee_id": a_id, "source": "flow", "flow_id": fid},
    )
    a = _staff_h("a@example.com", "staff-a")
    aid = client.get("/api/v1/staff/assignments", headers=a).json()[0]["id"]
    detail = client.get(f"/api/v1/staff/assignments/{aid}", headers=a).json()
    items = detail["items"]
    assert items[0]["status"] == "available" and items[1]["status"] == "locked"

    # complete item 0 by signing a response linked to its progress row
    prog0 = items[0]["progress_id"]
    r = client.post(
        "/api/v1/staff/form-responses",
        headers=a,
        json={"template_id": t1, "assignment_progress_id": prog0},
    ).json()
    client.post(
        f"/api/v1/staff/form-responses/{r['id']}/sign",
        headers=a,
        json={"signature_base64": "AAAA", "answers": {}},
    )
    after = client.get(f"/api/v1/staff/assignments/{aid}", headers=a).json()["items"]
    assert after[0]["status"] == "completed"
    assert after[1]["status"] == "available"  # sequential unlock fired


def test_flow_add_item_missing_target_404(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    h = _admin_h()
    flow = client.post("/api/v1/admin/forms/flows", headers=h, json={}).json()
    r = client.post(
        f"/api/v1/admin/forms/flows/{flow['id']}/items",
        headers=h,
        json={"item_type": "form", "item_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert r.status_code == 404


# --- courses: progress completes a standalone course assignment -------------


def test_course_progress_completes_assignment(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    a_id, _b = _seed_employees(client)
    h = _admin_h()
    course = client.post("/api/v1/admin/forms/courses", headers=h, json={"name_en": "Safety"}).json()
    cid = course["id"]
    sec = client.post(
        f"/api/v1/admin/forms/courses/{cid}/sections", headers=h, json={"title_en": "S1"}
    ).json()
    item = client.post(
        f"/api/v1/admin/forms/courses/sections/{sec['id']}/items",
        headers=h,
        json={"type": "text", "payload": {"body_en": "read me"}},
    ).json()
    client.post(f"/api/v1/admin/forms/courses/{cid}/publish", headers=h)

    client.post(
        "/api/v1/admin/forms/assignments",
        headers=h,
        json={"employee_id": a_id, "source": "standalone", "course_id": cid},
    )
    a = _staff_h("a@example.com", "staff-a")
    detail = client.get(
        f"/api/v1/staff/assignments/{client.get('/api/v1/staff/assignments', headers=a).json()[0]['id']}",
        headers=a,
    ).json()
    prog = detail["items"][0]["progress_id"]

    done = client.post(
        f"/api/v1/staff/course-progress/{cid}/item/{item['id']}/complete",
        headers=a,
        json={"assignment_progress_id": prog},
    )
    assert done.status_code == 200
    assert done.json()["course_complete"] is True

    aid = detail["id"]
    refreshed = client.get(f"/api/v1/staff/assignments/{aid}", headers=a).json()
    assert refreshed["items"][0]["status"] == "completed"


def test_course_progress_rejects_foreign_progress(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    a_id, b_id = _seed_employees(client)
    h = _admin_h()
    course = client.post("/api/v1/admin/forms/courses", headers=h, json={"name_en": "Safety"}).json()
    cid = course["id"]
    sec = client.post(
        f"/api/v1/admin/forms/courses/{cid}/sections", headers=h, json={"title_en": "S1"}
    ).json()
    item = client.post(
        f"/api/v1/admin/forms/courses/sections/{sec['id']}/items",
        headers=h,
        json={"type": "text", "payload": {}},
    ).json()
    client.post(f"/api/v1/admin/forms/courses/{cid}/publish", headers=h)
    # assignment belongs to B
    client.post(
        "/api/v1/admin/forms/assignments",
        headers=h,
        json={"employee_id": b_id, "source": "standalone", "course_id": cid},
    )
    b = _staff_h("b@example.com", "staff-b")
    b_aid = client.get("/api/v1/staff/assignments", headers=b).json()[0]["id"]
    b_prog = client.get(f"/api/v1/staff/assignments/{b_aid}", headers=b).json()["items"][0]["progress_id"]

    # A completes their own course progress but passes B's progress id -> B's item is NOT flipped
    a = _staff_h("a@example.com", "staff-a")
    a_done = client.post(
        f"/api/v1/staff/course-progress/{cid}/item/{item['id']}/complete",
        headers=a,
        json={"assignment_progress_id": b_prog},
    )
    assert a_done.status_code == 200
    # B's assignment item is still available (A could not flip it)
    b_after = client.get(f"/api/v1/staff/assignments/{b_aid}", headers=b).json()["items"][0]
    assert b_after["status"] == "available"
