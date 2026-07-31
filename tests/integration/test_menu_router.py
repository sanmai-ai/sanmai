"""Integration tests for the menu router via FastAPI TestClient.

Exercises the full request path — auth (StaticIdentityProvider), db (migrated sqlite),
and the LLM/Storage seams (echo/local fakes) — with zero external credentials. The
``get_session`` dependency is bound to a per-test migrated sqlite database; LLM/storage
are overridden with capturing fakes where the response shape matters.
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
from be.app.deps import get_llm, get_storage
from be.app.main import create_app
from be.db import get_session
from tests.conftest import admin_token, non_admin_token


class _FakeLLM(LLMProvider):
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
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
        self.calls.append({"system_prompt": system_prompt, "images": list(images or [])})
        return Completion(text=json.dumps(self._response), raw=self._response)


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


def test_requires_bearer_and_admin(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client = app_client
    # no header -> 401
    assert client.get("/api/v1/admin/menu/items/x/versions").status_code == 401
    # valid token, no admin claim -> 403
    r = client.get("/api/v1/admin/menu/items/x/versions", headers=_auth(non_admin_token()))
    assert r.status_code == 403


def test_version_save_list_restore_roundtrip(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client = app_client
    h = _auth(admin_token())

    r = client.post(
        "/api/v1/admin/menu/items/item-1/versions",
        headers=h,
        json={"snapshot": {"name": {"en": "Alpha"}, "active": True}},
    )
    assert r.status_code == 200, r.text
    v1 = r.json()["version"]
    assert v1["is_current"] is True
    assert v1["changed_by"] == "admin-1"  # Principal.uid, not email
    assert "active" not in v1["snapshot"]

    client.post(
        "/api/v1/admin/menu/items/item-1/versions",
        headers=h,
        json={"snapshot": {"name": {"en": "Beta"}}},
    )

    listing = client.get("/api/v1/admin/menu/items/item-1/versions", headers=h)
    versions = listing.json()["versions"]
    assert len(versions) == 2

    restore = client.post(
        f"/api/v1/admin/menu/items/item-1/versions/{v1['id']}/restore", headers=h
    )
    assert restore.status_code == 200
    assert restore.json()["version"]["snapshot"]["name"]["en"] == "Alpha"

    # restoring a missing version -> 404
    missing = client.post("/api/v1/admin/menu/items/item-1/versions/9999/restore", headers=h)
    assert missing.status_code == 404


def test_save_version_requires_name(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client = app_client
    r = client.post(
        "/api/v1/admin/menu/items/item-x/versions",
        headers=_auth(admin_token()),
        json={"snapshot": {"price": 10}},
    )
    assert r.status_code == 400


def test_version_uses_principal_venue(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client = app_client
    h = _auth(admin_token(venues=["venue-9"]))
    r = client.post(
        "/api/v1/admin/menu/items/item-v/versions",
        headers=h,
        json={"snapshot": {"name": {"en": "Scoped"}}},
    )
    assert r.json()["version"]["venue_id"] == "venue-9"


def test_recipe_put_get_and_validation(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client = app_client
    h = _auth(admin_token())

    put = client.put(
        "/api/v1/admin/menu/items/dish/recipe",
        headers=h,
        json={"lines": [{"inventory_sku": "S1", "location": "kitchen", "quantity": 2}]},
    )
    assert put.status_code == 200
    assert put.json()["lines"][0]["inventory_sku"] == "S1"

    got = client.get("/api/v1/admin/menu/items/dish/recipe", headers=h)
    assert got.json()["lines"][0]["quantity"] == 2.0

    bad = client.put(
        "/api/v1/admin/menu/items/dish/recipe",
        headers=h,
        json={"lines": [{"inventory_sku": "S1", "location": "freezer", "quantity": 2}]},
    )
    assert bad.status_code == 400


def test_translate_via_llm_seam(app_client) -> None:  # type: ignore[no-untyped-def]
    app, client = app_client
    fake = _FakeLLM({"text": "translated"})
    app.dependency_overrides[get_llm] = lambda: fake
    r = client.post(
        "/api/v1/admin/menu/translate",
        headers=_auth(admin_token()),
        json={"text": "hello", "source": "en", "target": "he", "field": "name"},
    )
    assert r.status_code == 200
    assert r.json()["text"] == "translated"
    assert "English" in fake.calls[0]["system_prompt"]  # heavy prompt threaded


def test_parse_ingredients_via_llm_seam(app_client) -> None:  # type: ignore[no-untyped-def]
    app, client = app_client
    fake = _FakeLLM({"en": "Chicken", "he": "עוף"})
    app.dependency_overrides[get_llm] = lambda: fake
    r = client.post(
        "/api/v1/admin/menu/parse-ingredients",
        headers=_auth(admin_token()),
        files={"file": ("label.png", b"\x89PNG-bytes", "image/png")},
    )
    assert r.status_code == 200
    assert r.json() == {"en": "Chicken", "he": "עוף"}
    assert fake.calls[0]["images"][0].mime_type == "image/png"


def test_upload_image_via_storage_seam(app_client) -> None:  # type: ignore[no-untyped-def]
    app, client = app_client
    store = LocalStorage()
    app.dependency_overrides[get_storage] = lambda: store
    r = client.post(
        "/api/v1/admin/menu/upload-image",
        headers=_auth(admin_token()),
        data={"item_id": "item-1", "name": "Chicken Dumpling", "slot": "6"},
        files={"file": ("pic.jpg", b"jpegdata", "image/jpeg")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["key"].startswith("menu/chicken-dumpling/6-")
    assert body["url"].startswith("file://")


def test_upload_image_rejects_non_image(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client = app_client
    r = client.post(
        "/api/v1/admin/menu/upload-image",
        headers=_auth(admin_token()),
        files={"file": ("x.txt", b"text", "text/plain")},
    )
    assert r.status_code == 400
