"""Pydantic request/response models for the menu domain.

Create-style models carry sensible defaults so a builder FE can ``POST {}`` to make
an empty draft (matches the house convention). Language codes are generic ``str`` —
the core is not Hebrew/English-specific.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TranslateTextRequest(BaseModel):
    """A single menu string to translate from ``source`` to ``target`` language."""

    text: str = Field(default="", max_length=4000)
    source: str = Field(default="")
    target: str = Field(default="")
    field: str = Field(default="name", description="name | description | ingredients")


class TranslateTextResponse(BaseModel):
    text: str


class ParseIngredientsResponse(BaseModel):
    en: str = ""
    he: str = ""


class UploadImageResponse(BaseModel):
    key: str
    url: str


class SaveVersionRequest(BaseModel):
    """Full content snapshot of the item. Availability flags are stripped server-side."""

    snapshot: dict[str, Any] = Field(default_factory=dict)


class MenuVersion(BaseModel):
    id: int
    venue_id: str
    item_id: str
    snapshot: dict[str, Any]
    changed_by: str | None = None
    changed_by_name: str | None = None
    source_version_id: int | None = None
    is_current: bool
    created_at: str | None = None


class VersionResponse(BaseModel):
    version: MenuVersion


class VersionListResponse(BaseModel):
    versions: list[MenuVersion]


class RecipeLine(BaseModel):
    option_index: int = -1  # -1 = item-level / single-price
    inventory_sku: str = ""
    location: str = ""
    quantity: float = 0.0


class SaveRecipeRequest(BaseModel):
    lines: list[RecipeLine] = Field(default_factory=list)


class RecipeResponse(BaseModel):
    lines: list[RecipeLine]


__all__ = [
    "TranslateTextRequest",
    "TranslateTextResponse",
    "ParseIngredientsResponse",
    "UploadImageResponse",
    "SaveVersionRequest",
    "MenuVersion",
    "VersionResponse",
    "VersionListResponse",
    "RecipeLine",
    "SaveRecipeRequest",
    "RecipeResponse",
]
