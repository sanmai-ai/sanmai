"""Integration tests for the inventory router via FastAPI TestClient.

Exercises the full request path — auth (StaticIdentityProvider), db (migrated sqlite),
and the LLM seam (fake) — with zero external credentials. ``get_session`` is bound to a
per-test migrated sqlite database; the LLM is overridden with a canned fake where the
parse-document response shape matters.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient

from be.adapters.llm.base import LLMProvider
from be.adapters.types import Completion, ImagePart
from be.app.deps import get_llm
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


def _create_item(client, sku="water", name="Water", locations=None) -> None:
    r = client.post(
        "/api/v1/inventory/items",
        headers=_auth(admin_token()),
        json={
            "sku": sku,
            "name": name,
            "category": "drinks",
            "unit": "bottle",
            "allowed_locations": locations or ["storage", "kitchen"],
        },
    )
    assert r.status_code == 200, r.text


def test_requires_bearer_and_admin(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client = app_client
    assert client.get("/api/v1/inventory/page").status_code == 401
    r = client.get("/api/v1/inventory/page", headers=_auth(non_admin_token()))
    assert r.status_code == 403
    r = client.post("/api/v1/suppliers", headers=_auth(non_admin_token()), json={"name": "X"})
    assert r.status_code == 403


def test_item_create_conflict_and_update(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client = app_client
    h = _auth(admin_token())
    _create_item(client)
    # duplicate -> 409
    r = client.post(
        "/api/v1/inventory/items",
        headers=h,
        json={"sku": "water", "name": "Water", "category": "d", "unit": "b", "allowed_locations": ["storage"]},
    )
    assert r.status_code == 409
    # bad location -> 400
    r = client.post(
        "/api/v1/inventory/items",
        headers=h,
        json={"name": "Ice", "category": "d", "unit": "b", "allowed_locations": ["igloo"]},
    )
    assert r.status_code == 400
    # update
    r = client.put("/api/v1/inventory/items/water", headers=h, json={"name": "Spring"})
    assert r.status_code == 200 and r.json()["name"] == "Spring"
    # update missing -> 404
    assert client.put("/api/v1/inventory/items/ghost", headers=h, json={"name": "x"}).status_code == 404
    # empty update -> 400
    assert client.put("/api/v1/inventory/items/water", headers=h, json={}).status_code == 400


def test_stock_update_and_move(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client = app_client
    h = _auth(admin_token())
    _create_item(client)
    r = client.post(
        "/api/v1/inventory/stock/update",
        headers=h,
        json={"sku": "water", "location": "storage", "quantity": 10},
    )
    assert r.status_code == 200
    # unknown sku -> 404
    r = client.post(
        "/api/v1/inventory/stock/update",
        headers=h,
        json={"sku": "ghost", "location": "storage", "quantity": 1},
    )
    assert r.status_code == 404
    # move
    r = client.post(
        "/api/v1/inventory/stock/move",
        headers=h,
        json={"sku": "water", "from_location": "storage", "to_location": "kitchen", "quantity": 4},
    )
    assert r.status_code == 200
    page = client.get("/api/v1/inventory/page", headers=h).json()
    row = next(x for x in page if x["sku"] == "water")
    assert row["stock"]["storage"] == 6.0 and row["stock"]["kitchen"] == 4.0
    # insufficient -> 400
    r = client.post(
        "/api/v1/inventory/stock/move",
        headers=h,
        json={"sku": "water", "from_location": "kitchen", "to_location": "foh", "quantity": 999},
    )
    assert r.status_code == 400


def test_supplier_and_supplier_item_flow(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client = app_client
    h = _auth(admin_token())
    _create_item(client)
    # POST {} rejected (name required) but POST with name works and slugs id
    assert client.post("/api/v1/suppliers", headers=h, json={}).status_code == 400
    r = client.post(
        "/api/v1/suppliers",
        headers=h,
        json={"name": "Acme Co", "categories": ["drinks"], "order_cutoff_time": "9:05"},
    )
    assert r.status_code == 200, r.text
    supplier = r.json()
    assert supplier["supplier_id"] == "acme_co"
    assert supplier["order_cutoff_time"] == "09:05"  # normalized

    # bad cutoff -> 400
    assert client.post(
        "/api/v1/suppliers", headers=h, json={"name": "Y", "order_cutoff_time": "nope"}
    ).status_code == 400

    r = client.post(
        "/api/v1/inventory/supplier-items/water",
        headers=h,
        json={"supplier_id": "acme_co", "supplier_sku": "W1", "pack_size": 24, "price_per_pack": 30},
    )
    assert r.status_code == 200, r.text
    assert client.get("/api/v1/suppliers", headers=h).json()[0]["name"] == "Acme Co"

    r = client.put(
        "/api/v1/inventory/supplier-items/water/acme_co",
        headers=h,
        json={"is_on_stop": True},
    )
    assert r.status_code == 200 and r.json()["is_on_stop"] is True


def test_order_place_receive_and_expected(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client = app_client
    h = _auth(admin_token())
    _create_item(client)
    r = client.post(
        "/api/v1/orders/placed",
        headers=h,
        json={
            "order_id": "po1",
            "supplier_id": "acme",
            "supplier_name": "Acme",
            "expected_delivery_date": "2026-08-05",
            "items": [{"inventory_sku": "water", "packs": 2, "pack_size": 24, "pack_price": 30}],
        },
    )
    assert r.status_code == 200
    # bad pack size -> 400
    r = client.post(
        "/api/v1/orders/placed",
        headers=h,
        json={"order_id": "po2", "supplier_id": "acme", "supplier_name": "A",
              "items": [{"inventory_sku": "water", "packs": 1, "pack_size": 0}]},
    )
    assert r.status_code == 400

    r = client.post(
        "/api/v1/orders/received",
        headers=h,
        json={"order_id": "po1", "items": [{"inventory_sku": "water", "quantity": 48}]},
    )
    assert r.status_code == 200
    page = client.get("/api/v1/inventory/page", headers=h).json()
    row = next(x for x in page if x["sku"] == "water")
    assert row["stock"]["storage"] == 48.0


def test_parse_document_via_llm_seam(app_client) -> None:  # type: ignore[no-untyped-def]
    app, client = app_client
    h = _auth(admin_token())
    _create_item(client)
    fake = _FakeLLM({"lines": [{"raw_name": "Water 500", "quantity": 2, "matched_index": 0}]})
    app.dependency_overrides[get_llm] = lambda: fake

    r = client.post(
        "/api/v1/orders/parse-document",
        headers=h,
        data={"supplier_id": ""},
        files={"file": ("order.png", b"\x89PNG-bytes", "image/png")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lines"][0]["inventory_sku"] == "water"  # enriched from catalog
    assert fake.calls[0]["images"][0].mime_type == "image/png"

    # unsupported mime -> 415
    r = client.post(
        "/api/v1/orders/parse-document",
        headers=h,
        files={"file": ("x.txt", b"text", "text/plain")},
    )
    assert r.status_code == 415


def test_parse_document_bad_model_returns_502(app_client) -> None:  # type: ignore[no-untyped-def]
    app, client = app_client
    fake = _FakeLLM({})  # no "lines" -> empty parse -> None
    app.dependency_overrides[get_llm] = lambda: fake
    r = client.post(
        "/api/v1/orders/parse-document",
        headers=_auth(admin_token()),
        files={"file": ("order.png", b"\x89PNG", "image/png")},
    )
    assert r.status_code == 502


def test_import_aliases_and_payments(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, client = app_client
    h = _auth(admin_token())
    _create_item(client)
    r = client.post(
        "/api/v1/orders/import-aliases",
        headers=h,
        json={
            "supplier_id": "acme",
            "mappings": [
                {"raw_name": "Ferrarelle 500", "inventory_sku": "water"},
                {"raw_name": "Junk", "inventory_sku": "nope"},
            ],
        },
    )
    assert r.status_code == 200 and r.json()["upserted"] == 1

    # missing supplier_id -> 400
    assert client.post(
        "/api/v1/orders/import-aliases", headers=h, json={"mappings": []}
    ).status_code == 400

    # supplier monthly payment
    r = client.post(
        "/api/v1/suppliers/acme/payments",
        headers=h,
        json={"period_year": 2026, "period_month": 8, "status": "paid", "paid_amount": 100},
    )
    assert r.status_code == 200 and r.json()["status"] == "paid"
    listing = client.get("/api/v1/suppliers/acme/payments", headers=h).json()
    assert len(listing) == 1
