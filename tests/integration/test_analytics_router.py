"""Integration tests for the analytics router via FastAPI TestClient.

Full request path — auth (StaticIdentityProvider -> DB-RBAC), db (migrated sqlite),
fake LLM + LocalStorage — zero external credentials.

Focus:
* admin-gating regression: every analytics route requires ``require_admin("analytics")``
  — 401 without a token, 403 for a non-admin and for an admin lacking the page; a spoofed
  ``?email=`` query param is ignored (the live email-spoof hole is closed);
* the ``X-Ingest-Token`` server-to-server upload path (shared secret, no Bearer);
* an ingest + read roundtrip over a tiny in-memory CSV (+ idempotent re-upload) and the
  auth-gated archived-file proxy download;
* the text-to-SQL chat is read-only/safe: a mocked LLM that emits DROP/multi-statement/
  off-catalog SQL is REJECTED before execution (logged ``rejected``, DB unchanged), while
  a benign SELECT runs and returns rows.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient

from be.adapters.llm.base import LLMProvider
from be.adapters.storage.local import LocalStorage
from be.adapters.types import Completion, ImagePart
from be.app.deps import get_llm, get_settings_dep, get_storage
from be.app.main import create_app
from be.config import get_settings
from be.db import get_session
from tests.conftest import admin_token, non_admin_token, seed_admin_user

_CSV = (
    b"order_id,order_date,order_ts,dish_type,dish_name,menu_price,sale_price,qty,cost_sum\n"
    b"1,2026-01-05,2026-01-05T12:30:00,Mains,Ramen,50,50,1,20\n"
    b"1,2026-01-05,2026-01-05T12:30:00,Sides,Edamame,15,15,2,4\n"
    b"2,2026-01-06,2026-01-06T19:00:00,Mains,Ramen,50,45,1,20\n"
)

INGEST_TOKEN = "s3cret-token"


class _FakeLLM(LLMProvider):
    """Returns a canned dict for schema (structured) calls and text for free-text calls."""

    def __init__(self, structured: dict[str, Any], answer: str = "Here you go.") -> None:
        self._structured = structured
        self._answer = answer
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        schema: dict[str, Any] | None = None,
        images: Sequence[ImagePart] | None = None,
        model: str | None = None,
        temperature: float | None = None,
    ) -> Completion:
        self.calls.append({"schema": bool(schema)})
        if schema:
            return Completion(text=json.dumps(self._structured), raw=self._structured)
        return Completion(text=self._answer, raw=None)


@pytest.fixture
def app_client(sessionmaker_for, migrated_db):  # type: ignore[no-untyped-def]
    app = create_app()
    sm = sessionmaker_for

    async def _override_session() -> Any:
        async with sm() as session:
            yield session

    shared_storage = LocalStorage()
    tokened = get_settings().model_copy(update={"analytics_ingest_token": INGEST_TOKEN})
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_storage] = lambda: shared_storage
    app.dependency_overrides[get_settings_dep] = lambda: tokened
    client = TestClient(app)
    try:
        yield app, client, migrated_db
    finally:
        app.dependency_overrides.clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _admin_h() -> dict[str, str]:
    return _auth(admin_token())


def _set_llm(app, llm: LLMProvider) -> None:
    app.dependency_overrides[get_llm] = lambda: llm


def _upload(client: TestClient, headers: dict[str, str], data: bytes = _CSV) -> Any:
    return client.post(
        "/api/v1/analytics/upload", headers=headers,
        files={"file": ("sales.csv", data, "text/csv")},
    )


# --- access control ---------------------------------------------------------


def test_no_token_is_401(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    assert client.get("/api/v1/analytics/summary").status_code == 401
    assert client.get("/api/v1/analytics/ingestions").status_code == 401
    assert client.post("/api/v1/analytics/chat", json={"question": "x"}).status_code == 401
    # upload with no token and no admin bearer -> 401 (token path disabled without header)
    assert _upload(client, {}).status_code == 401


def test_admin_page_gate(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, url = app_client

    # non-admin (no admin_users row) -> 403
    non = _auth(non_admin_token(uid="u-x", email="x@example.com"))
    assert client.get("/api/v1/analytics/summary", headers=non).status_code == 403

    # admin WITHOUT the analytics page -> 403
    seed_admin_user(
        url, admin_id=2, uid="mgr-1", email="mgr@example.test", role="manager",
        allowed_pages=["menu"],
    )
    mgr = _auth(admin_token(uid="mgr-1", email="mgr@example.test"))
    assert client.get("/api/v1/analytics/summary", headers=mgr).status_code == 403

    # admin WITH the analytics page -> 200
    seed_admin_user(
        url, admin_id=3, uid="an-1", email="an@example.test", role="manager",
        allowed_pages=["analytics"],
    )
    an = _auth(admin_token(uid="an-1", email="an@example.test"))
    assert client.get("/api/v1/analytics/summary", headers=an).status_code == 200

    # default full_admin bypasses the page gate -> 200
    assert client.get("/api/v1/analytics/summary", headers=_admin_h()).status_code == 200


def test_spoofed_email_query_param_is_ignored(app_client) -> None:  # type: ignore[no-untyped-def]
    """Regression: authority comes from the verified token, never from ?email=."""
    _app, client, _url = app_client
    non = _auth(non_admin_token(uid="u-x", email="x@example.com"))
    # a non-admin naming the seeded full_admin in the query string is still 403
    r = client.get(
        "/api/v1/analytics/summary?email=admin@example.test&phone=+972500000000",
        headers=non,
    )
    assert r.status_code == 403


# --- X-Ingest-Token server-to-server path -----------------------------------


def test_ingest_token_uploads_without_bearer(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    r = _upload(client, {"X-Ingest-Token": INGEST_TOKEN})
    assert r.status_code == 200, r.text
    assert r.json()["rows_inserted"] == 3
    # tagged source='email'
    rows = client.get("/api/v1/analytics/ingestions", headers=_admin_h()).json()
    assert rows[0]["source"] == "email"


def test_wrong_ingest_token_falls_to_admin_gate(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    # wrong token + no bearer -> 401
    assert _upload(client, {"X-Ingest-Token": "nope"}).status_code == 401
    # wrong token + non-admin bearer -> 403
    non = _auth(non_admin_token(uid="u-x", email="x@example.com"))
    assert _upload(client, {**non, "X-Ingest-Token": "nope"}).status_code == 403


# --- ingest + read roundtrip ------------------------------------------------


def test_ingest_read_roundtrip_and_idempotency(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    h = _admin_h()

    r = _upload(client, h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows_inserted"] == 3 and body["rows_skipped"] == 0
    ing_id = body["ingestion_id"]

    summ = client.get(
        "/api/v1/analytics/summary?from=2026-01-01&to=2026-01-31", headers=h
    ).json()
    assert summ["revenue"] == 110.0  # 50 + 15 + 45
    assert summ["orders"] == 2
    assert summ["gross_margin"] == 66.0  # 110 - (20+4+20)

    ts = client.get(
        "/api/v1/analytics/timeseries?from=2026-01-01&to=2026-01-31&granularity=day",
        headers=h,
    ).json()
    assert {row["period"] for row in ts} == {"2026-01-05", "2026-01-06"}

    top = client.get(
        "/api/v1/analytics/top-dishes?from=2026-01-01&to=2026-01-31", headers=h
    ).json()
    assert any(d["pos_name"] == "Ramen" for d in top)

    heat = client.get(
        "/api/v1/analytics/heatmap?from=2026-01-01&to=2026-01-31", headers=h
    ).json()
    assert {(x["dow"], x["hour"]) for x in heat} == {(1, 12), (2, 19)}

    # idempotent re-upload -> everything skipped
    again = _upload(client, h).json()
    assert again["rows_inserted"] == 0 and again["rows_skipped"] == 3

    # archived file is proxy-streamed back through the auth-gated endpoint
    got = client.get(f"/api/v1/analytics/ingestions/{ing_id}/file", headers=h)
    assert got.status_code == 200 and got.content == _CSV
    # and requires auth
    assert client.get(f"/api/v1/analytics/ingestions/{ing_id}/file").status_code == 401


def test_reprocess_rebuilds_sales(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client, _url = app_client
    h = _admin_h()
    ing_id = _upload(client, h).json()["ingestion_id"]
    r = client.post(
        "/api/v1/analytics/reprocess", headers=h, json={"ingestion_id": ing_id}
    )
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 3
    # sales still add up after the rebuild
    summ = client.get(
        "/api/v1/analytics/summary?from=2026-01-01&to=2026-01-31", headers=h
    ).json()
    assert summ["revenue"] == 110.0


# --- text-to-SQL chat: read-only / safe -------------------------------------


def test_chat_benign_select_runs(app_client) -> None:  # type: ignore[no-untyped-def]
    app, client, _url = app_client
    _upload(client, _admin_h())
    _set_llm(app, _FakeLLM(
        {"sql": "SELECT COUNT(*) AS n FROM analytics_sales"}, answer="There are rows."
    ))
    r = client.post("/api/v1/analytics/chat", headers=_admin_h(), json={"question": "how many?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "There are rows."
    assert body["row_count"] == 1 and body["rows"][0]["n"] == 3
    # logged as ok
    runs = client.get("/api/v1/analytics/processing-runs", headers=_admin_h()).json()
    assert any(x["kind"] == "chat" and x["status"] == "ok" for x in runs)


@pytest.mark.parametrize(
    "evil_sql",
    [
        "DROP TABLE analytics_sales",
        "SELECT 1; DELETE FROM analytics_sales",
        "SELECT * FROM admin_users",
        "UPDATE analytics_sales SET qty = 0",
    ],
)
def test_chat_rejects_unsafe_sql_and_does_not_mutate(app_client, evil_sql) -> None:  # type: ignore[no-untyped-def]
    app, client, _url = app_client
    _upload(client, _admin_h())
    before = client.get(
        "/api/v1/analytics/summary?from=2026-01-01&to=2026-01-31", headers=_admin_h()
    ).json()["revenue"]

    _set_llm(app, _FakeLLM({"sql": evil_sql}))
    r = client.post(
        "/api/v1/analytics/chat", headers=_admin_h(), json={"question": "drop it"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == []  # nothing executed

    # run logged as rejected and the data is untouched
    runs = client.get("/api/v1/analytics/processing-runs", headers=_admin_h()).json()
    assert any(x["kind"] == "chat" and x["status"] == "rejected" for x in runs)
    after = client.get(
        "/api/v1/analytics/summary?from=2026-01-01&to=2026-01-31", headers=_admin_h()
    ).json()["revenue"]
    assert after == before == 110.0
