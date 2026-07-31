"""Menu LLM-backed helpers, wired through the :class:`LLMProvider` seam only.

No vendor imports: translation and ingredient parsing call
``LLMProvider.complete(..., system_prompt=..., schema=..., images=[ImagePart(...)])``.
The heavy instruction rides in ``system_prompt`` (the revised core contract) and image
inputs carry an explicit MIME via :class:`ImagePart` — fixing the Increment-1 wrapper
gaps where the system prompt was dropped and MIME was sniffed.

Prompts are generic (no cuisine/brand strings). Structured mode uses a provider-native
response schema; the returned ``Completion.text`` is parsed as JSON.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from be.adapters.llm.base import LLMProvider
from be.adapters.types import ImagePart

_FIELD_CONTEXT = {
    "name": "menu item name",
    "description": "menu item description",
    "ingredients": "menu item ingredient list",
}

_TRANSLATE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {"text": {"type": "STRING"}},
    "required": ["text"],
}

_INGREDIENTS_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {"en": {"type": "STRING"}, "he": {"type": "STRING"}},
    "required": ["en"],
}


def _lang_name(code: str) -> str:
    return {"he": "Hebrew", "en": "English"}.get(code.lower(), code)


def _parse_json(text: str) -> dict:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def translate_text(
    llm: LLMProvider,
    *,
    text: str,
    source: str,
    target: str,
    field: str = "name",
) -> str | None:
    """Translate one short menu string ``source`` -> ``target``. ``None`` on failure."""
    context = _FIELD_CONTEXT.get(field, "menu item text")
    src, tgt = _lang_name(source), _lang_name(target)
    system_prompt = (
        f"You translate short {context} text for a restaurant's menu from {src} to "
        f"{tgt}. Return a faithful, natural, concise {tgt} translation that reads well "
        "on a menu. Preserve the tone and any dish/brand names that are normally left "
        "untranslated. Do NOT add quotes, notes, or extra text. Return ONLY the "
        'translated string in the JSON field "text".'
    )
    completion = await llm.complete(
        text.strip(),
        system_prompt=system_prompt,
        schema=_TRANSLATE_SCHEMA,
    )
    result = _parse_json(completion.text)
    value = result.get("text")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


async def parse_ingredients(
    llm: LLMProvider,
    *,
    image: bytes,
    mime_type: str,
) -> dict | None:
    """Extract a bilingual ingredient list from a label photo. ``None`` on failure."""
    system_prompt = (
        "You are given a photo of a food product's ingredient list or label. Extract "
        "EVERY ingredient faithfully — do NOT invent, omit, or guess. Return a clean, "
        "human-readable list as a single comma-separated string. Capitalize each "
        "ingredient; keep parenthetical sub-ingredients and allergen notes. Provide BOTH "
        "an English list (`en`) and a Hebrew list (`he`) — if the label is in one "
        "language, the other is a faithful translation. Return ONLY the JSON object."
    )
    completion = await llm.complete(
        "Extract the full ingredient list from the attached photo.",
        system_prompt=system_prompt,
        schema=_INGREDIENTS_SCHEMA,
        images=[ImagePart(data=image, mime_type=mime_type)],
    )
    result = _parse_json(completion.text)
    en = (result.get("en") or "").strip() if isinstance(result.get("en"), str) else ""
    he = (result.get("he") or "").strip() if isinstance(result.get("he"), str) else ""
    if en or he:
        return {"en": en, "he": he}
    return None


def _slug(value: str | None, default: str = "x") -> str:
    """Lowercase dash-separated filesystem-safe token for storage object keys."""
    out = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return out[:40] or default


_EXT_BY_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


def build_image_key(
    *,
    item_id: str,
    name: str,
    slot: str,
    content_type: str,
    filename: str | None = None,
    now: Callable[[], datetime] = datetime.now,
) -> str:
    """Human-readable, unique storage key: ``menu/<item>/<slot>-<ts>-<short>.<ext>``.

    Unique per upload so older versions keep their images (restore stays valid).
    ``now`` is injectable for deterministic tests.
    """
    folder = _slug(name) if name.strip() else _slug(item_id, "misc")
    ext = _EXT_BY_TYPE.get(content_type.lower())
    if not ext:
        tail = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
        ext = re.sub(r"[^a-z0-9]", "", tail)[:5] or "img"
    ts = now().strftime("%Y%m%d-%H%M%S")
    return f"menu/{folder}/{_slug(slot, 'img')}-{ts}-{uuid.uuid4().hex[:6]}.{ext}"


__all__ = ["translate_text", "parse_ingredients", "build_image_key"]
