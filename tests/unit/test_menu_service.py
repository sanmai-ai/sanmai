"""Unit tests for menu service helpers + crud pure functions (no DB, no creds)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pytest

from be.adapters.llm.base import LLMProvider
from be.adapters.llm.echo import EchoLLMProvider
from be.adapters.types import Completion, ImagePart
from be.app.domains.menu import crud, service


class RecordingLLM(LLMProvider):
    """Fake LLM that records the exact call and returns a canned JSON completion."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response

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
                "images": list(images) if images else [],
                "model": model,
                "temperature": temperature,
            }
        )
        return Completion(text=json.dumps(self._response), raw=self._response)


# ------------------------------------------------------------------ service: translate


async def test_translate_threads_system_prompt_and_schema() -> None:
    llm = RecordingLLM({"text": "שלום"})
    out = await service.translate_text(llm, text="hello", source="en", target="he", field="name")
    assert out == "שלום"
    call = llm.calls[0]
    assert call["prompt"] == "hello"
    assert call["schema"] == {"type": "OBJECT", "properties": {"text": {"type": "STRING"}}, "required": ["text"]}
    assert "English" in call["system_prompt"] and "Hebrew" in call["system_prompt"]
    assert "menu item name" in call["system_prompt"]


async def test_translate_returns_none_when_no_text() -> None:
    # EchoLLMProvider returns {"echo": prompt} in schema mode -> no "text" field.
    assert await service.translate_text(EchoLLMProvider(), text="x", source="en", target="he") is None


# --------------------------------------------------------------- service: parse-ingredients


async def test_parse_ingredients_threads_imagepart() -> None:
    llm = RecordingLLM({"en": "Chicken, Cabbage", "he": "עוף, כרוב"})
    out = await service.parse_ingredients(llm, image=b"\x89PNG-bytes", mime_type="image/png")
    assert out == {"en": "Chicken, Cabbage", "he": "עוף, כרוב"}
    images = llm.calls[0]["images"]
    assert len(images) == 1
    assert isinstance(images[0], ImagePart)
    assert images[0].mime_type == "image/png"
    assert images[0].data == b"\x89PNG-bytes"


async def test_parse_ingredients_none_when_empty() -> None:
    assert await service.parse_ingredients(RecordingLLM({}), image=b"x", mime_type="image/png") is None


# --------------------------------------------------------------------- service: image key


def test_build_image_key_is_readable_and_scoped() -> None:
    key = service.build_image_key(
        item_id="abc123",
        name="Chicken Dumpling",
        slot="6",
        content_type="image/jpeg",
        now=lambda: datetime(2026, 7, 31, 20, 15, 30),
    )
    assert key.startswith("menu/chicken-dumpling/6-20260731-201530-")
    assert key.endswith(".jpg")


def test_build_image_key_falls_back_to_filename_ext() -> None:
    key = service.build_image_key(
        item_id="x",
        name="",
        slot="item",
        content_type="application/octet-stream",
        filename="photo.HEIC",
        now=lambda: datetime(2026, 1, 1),
    )
    assert key.startswith("menu/x/item-20260101-000000-")
    assert key.endswith(".heic")


# ------------------------------------------------------------------------- crud: snapshot


def test_clean_snapshot_whitelist_and_option_active_strip() -> None:
    raw = {
        "name": {"en": "A"},
        "price": 42,
        "active": True,  # availability flag -> dropped
        "is_available_takeaway": False,  # dropped
        "id": "doc-1",  # doc key -> dropped
        "options": [{"size": "6", "price": 30, "active": False}, "raw-string"],
        "junk": "nope",  # not whitelisted -> dropped
    }
    snap = crud.clean_snapshot(raw)
    assert set(snap) == {"name", "price", "options"}
    assert snap["options"][0] == {"size": "6", "price": 30}  # option 'active' stripped
    assert snap["options"][1] == "raw-string"


def test_clean_snapshot_non_dict() -> None:
    assert crud.clean_snapshot(None) == {}
    assert crud.clean_snapshot("nope") == {}  # type: ignore[arg-type]


def test_has_content() -> None:
    assert crud.has_content({"name": {"en": "A"}})
    assert not crud.has_content({})
    assert not crud.has_content({"active": True})  # availability is not content


# --------------------------------------------------------------------- crud: validate line


def test_validate_line_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="inventory_sku"):
        crud._validate_line({"inventory_sku": "", "location": "kitchen", "quantity": 1})
    with pytest.raises(ValueError, match="location"):
        crud._validate_line({"inventory_sku": "S", "location": "freezer", "quantity": 1})
    with pytest.raises(ValueError, match="quantity must be > 0"):
        crud._validate_line({"inventory_sku": "S", "location": "kitchen", "quantity": 0})
    with pytest.raises(ValueError, match="quantity must be a number"):
        crud._validate_line({"inventory_sku": "S", "location": "kitchen", "quantity": "abc"})


def test_validate_line_coerces_ok() -> None:
    line = crud._validate_line(
        {"inventory_sku": " S1 ", "location": "kitchen", "quantity": "2.5", "option_index": "0"}
    )
    assert line["inventory_sku"] == "S1"
    assert line["option_index"] == 0
    assert str(line["quantity"]) == "2.5"
