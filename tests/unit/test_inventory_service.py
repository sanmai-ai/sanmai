"""Unit tests for the order-import service — no DB engine, LLM fake, zero creds.

Covers normalize_name, the LLM-seam threading (schema + ImagePart + system prompt),
deterministic alias-override-wins, authoritative catalog enrichment, suggested_new
sanitisation, and the None-on-failure path. The DB is a tiny in-memory fake so these
stay pure-unit (crud integration is covered separately).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from be.adapters.errors import ProviderTransient
from be.adapters.llm.base import LLMProvider
from be.adapters.types import Completion, ImagePart
from be.app.domains.inventory import service


class _RecordingLLM(LLMProvider):
    def __init__(self, response: dict[str, Any] | None) -> None:
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
        self.calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "schema": schema,
                "images": list(images or []),
                "model": model,
                "temperature": temperature,
            }
        )
        text = json.dumps(self._response) if self._response is not None else "not json"
        return Completion(text=text, raw=self._response)


class _RaisingLLM(LLMProvider):
    async def complete(self, prompt: str, **kwargs: Any) -> Completion:
        raise ProviderTransient("model unavailable")


class _FakeSession:
    """Records nothing — the service reaches crud only through the monkeypatched fns."""


def _patch_crud(monkeypatch, *, catalog: list[dict], aliases: dict[str, str]) -> None:
    async def _catalog(session, *, supplier_id):
        return catalog

    async def _aliases(session, *, supplier_id):
        return aliases

    monkeypatch.setattr(service.crud, "get_order_match_catalog", _catalog)
    monkeypatch.setattr(service.crud, "get_order_import_aliases", _aliases)


_CATALOG = [
    {
        "inventory_sku": "water_500",
        "name": "Sparkling water 500ml",
        "unit": "bottle",
        "category": "drinks",
        "supplier_sku": "SUP-W500",
        "pack_name": "case",
        "pack_size": 24.0,
        "price_per_pack": 30.0,
        "from_supplier": True,
    },
    {
        "inventory_sku": "napkins",
        "name": "Napkins",
        "unit": "pack",
        "category": "disposable",
        "supplier_sku": None,
        "pack_name": None,
        "pack_size": None,
        "price_per_pack": None,
        "from_supplier": False,
    },
]


def test_normalize_name() -> None:
    assert service.normalize_name("  Cola   Classic ") == "cola classic"
    assert service.normalize_name(None) == ""
    assert service.normalize_name("ABC") == "abc"


async def test_parse_threads_schema_image_and_enriches(monkeypatch) -> None:
    _patch_crud(monkeypatch, catalog=_CATALOG, aliases={})
    # index 0 = supplier item water_500 (supplier items first)
    llm = _RecordingLLM(
        {"lines": [{"raw_name": "Ferrarelle 500", "quantity": 2, "matched_index": 0}]}
    )
    out = await service.parse_order_document(
        llm, _FakeSession(), data=b"\x89PNG", mime="image/png", supplier_id="sup1"
    )
    assert out is not None
    call = llm.calls[0]
    assert call["schema"] == service.PARSE_RESPONSE_SCHEMA
    assert call["temperature"] == 0
    assert isinstance(call["images"][0], ImagePart)
    assert call["images"][0].mime_type == "image/png"
    assert call["images"][0].data == b"\x89PNG"

    line = out["lines"][0]
    assert line["matched"] is True
    assert line["match_source"] == "ai"
    # Authoritative enrichment from catalog — never the model echo.
    assert line["inventory_sku"] == "water_500"
    assert line["inventory_name"] == "Sparkling water 500ml"
    assert line["unit"] == "bottle"
    assert line["pack_size"] == 24.0
    assert line["price_per_pack"] == 30.0


async def test_alias_override_beats_model(monkeypatch) -> None:
    # Model says match index 1 (napkins); alias forces water_500 for that raw name.
    _patch_crud(
        monkeypatch, catalog=_CATALOG, aliases={"ferrarelle 500": "water_500"}
    )
    llm = _RecordingLLM(
        {"lines": [{"raw_name": "Ferrarelle 500", "quantity": 1, "matched_index": 1}]}
    )
    out = await service.parse_order_document(
        llm, _FakeSession(), data=b"x", mime="image/png", supplier_id="sup1"
    )
    assert out is not None
    line = out["lines"][0]
    assert line["match_source"] == "alias"
    assert line["inventory_sku"] == "water_500"
    assert line["match_confidence"] == 1.0


async def test_no_match_builds_suggested_new(monkeypatch) -> None:
    _patch_crud(monkeypatch, catalog=_CATALOG, aliases={})
    llm = _RecordingLLM(
        {
            "lines": [
                {
                    "raw_name": "Mystery gadget",
                    "quantity": 3,
                    "matched_index": -1,
                    "suggested_new": {
                        "name": "Mystery gadget",
                        "category": "tools",
                        "unit": "pcs",
                        "pack_size": 6,
                    },
                }
            ]
        }
    )
    out = await service.parse_order_document(
        llm, _FakeSession(), data=b"x", mime="image/png", supplier_id="sup1"
    )
    assert out is not None
    line = out["lines"][0]
    assert line["matched"] is False
    assert line["inventory_sku"] is None
    # Category is free text (no fixed core taxonomy).
    assert line["suggested_new"] == {
        "name": "Mystery gadget",
        "category": "tools",
        "unit": "pcs",
        "pack_size": 6.0,
    }


async def test_returns_none_when_model_fails(monkeypatch) -> None:
    _patch_crud(monkeypatch, catalog=_CATALOG, aliases={})
    out = await service.parse_order_document(
        _RaisingLLM(), _FakeSession(), data=b"x", mime="image/png", supplier_id="sup1"
    )
    assert out is None


async def test_returns_none_when_model_returns_garbage(monkeypatch) -> None:
    _patch_crud(monkeypatch, catalog=_CATALOG, aliases={})
    out = await service.parse_order_document(
        _RecordingLLM(None), _FakeSession(), data=b"x", mime="image/png", supplier_id="sup1"
    )
    assert out is None


async def test_model_fallback_second_model_used(monkeypatch) -> None:
    _patch_crud(monkeypatch, catalog=_CATALOG, aliases={})

    class _FailThenSucceed(LLMProvider):
        def __init__(self) -> None:
            self.models: list[str | None] = []

        async def complete(self, prompt: str, *, model: str | None = None, **kw: Any) -> Completion:
            self.models.append(model)
            if model == "a":
                raise ProviderTransient("down")
            return Completion(
                text=json.dumps({"lines": [{"raw_name": "x", "matched_index": -1}]}),
                raw=None,
            )

    llm = _FailThenSucceed()
    out = await service.parse_order_document(
        llm, _FakeSession(), data=b"x", mime="image/png", supplier_id="s", models=("a", "b")
    )
    assert out is not None
    assert llm.models == ["a", "b"]
