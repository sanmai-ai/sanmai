"""Pydantic request models for the analytics (sales pipeline) domain.

Create/action models carry sensible defaults so a builder FE can ``POST {}`` (matches
the house convention); the router validates the few genuinely-required fields. Nothing
here is cuisine/brand/locale-specific — the per-line breakdown is a generic
``components`` array of ``{"name", "count"}``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """A natural-language question over the sales data (text-to-SQL)."""

    question: str = Field(default="")
    history: list[dict[str, Any]] = Field(default_factory=list)


class ReprocessRequest(BaseModel):
    """Rebuild processed sales from raw — by one upload or a date range."""

    ingestion_id: int | None = None
    date_from: str | None = None  # ISO 'YYYY-MM-DD'
    date_to: str | None = None


class MenuSyncRequest(BaseModel):
    """Reconcile the dish catalog against un-enriched sales via the LLM seam."""

    dry_run: bool = True


class MenuItemPayload(BaseModel):
    """One menu-catalog mirror row."""

    id: str = Field(default="")
    name: str | None = None
    category: str | None = None
    price: float | None = None
    active: bool = True
    payload: dict[str, Any] = Field(default_factory=dict)


class MenuItemsSyncRequest(BaseModel):
    items: list[MenuItemPayload] = Field(default_factory=list)


class SuggestionResolveRequest(BaseModel):
    """Approve or dismiss an AI dish suggestion (actor may override the proposal)."""

    status: str = Field(default="approved")  # 'approved' | 'dismissed'
    name: str | None = None
    category: str | None = None
    components: list[dict[str, Any]] | None = None


__all__ = [
    "ChatRequest",
    "ReprocessRequest",
    "MenuSyncRequest",
    "MenuItemPayload",
    "MenuItemsSyncRequest",
    "SuggestionResolveRequest",
]
