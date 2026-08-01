"""Pydantic request/response models for the suggestions domain.

Create-style models carry sensible defaults (house convention: a builder FE can
``POST {}`` for a draft). Staff action payloads DELIBERATELY do NOT carry an
``email``/``phone`` identity: the acting employee is resolved from the VERIFIED
token by the router (this closes the live query-param/body impersonation holes).
"""

from __future__ import annotations

from pydantic import BaseModel


class SuggestionCreate(BaseModel):
    title: str = "Untitled suggestion"
    category: str = "other"  # 'foh' | 'kitchen' | 'storage' | 'system' | 'other'
    description: str = ""


class CommentCreate(BaseModel):
    body: str = ""


class AdminUpdate(BaseModel):
    is_hidden: bool | None = None
    status: str | None = None  # 'open' | 'closed'


class SettingsUpdate(BaseModel):
    notification_email: str = ""


class PlusOneResponse(BaseModel):
    plus_one_count: int
    viewer_plus_oned: bool


__all__ = [
    "SuggestionCreate",
    "CommentCreate",
    "AdminUpdate",
    "SettingsUpdate",
    "PlusOneResponse",
]
