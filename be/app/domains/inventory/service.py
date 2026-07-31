"""Order-import service — parse a supplier order/invoice document into matched lines.

Wired through the :class:`LLMProvider` seam only (no vendor imports). The document is
parse-only (never stored). The flow mirrors the live agent:

  1. build the authoritative match catalog (crud);
  2. consult the learned per-supplier aliases (crud);
  3. ask the model to extract lines + match each to a catalog INDEX, seeded with the
     known aliases as hints (``LLMProvider.complete`` with a provider-native schema and
     an :class:`ImagePart` carrying the raw bytes + explicit MIME);
  4. DETERMINISTICALLY override with the learned aliases (alias always wins);
  5. authoritatively enrich each matched line from the catalog by sku — the model's
     echoes of name/pack/price are never trusted.

The prompt is generic (no brand/locale/tax/deposit strings): it works for any
Hebrew/English (or other-language) supplier document. ``suggested_new.category`` is
passed through as free text (the core has no fixed category taxonomy).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from be.adapters.errors import AdapterError
from be.adapters.llm.base import LLMProvider
from be.adapters.types import ImagePart
from be.app.domains.inventory import crud

logger = logging.getLogger(__name__)

# Provider-native response schema (uppercase types) for the extracted line items.
PARSE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "lines": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "raw_name": {"type": "STRING"},
                    "quantity": {"type": "NUMBER"},
                    "unit_price": {"type": "NUMBER"},
                    "line_total": {"type": "NUMBER"},
                    "matched_index": {"type": "INTEGER"},
                    "match_confidence": {"type": "NUMBER"},
                    "suggested_new": {
                        "type": "OBJECT",
                        "properties": {
                            "name": {"type": "STRING"},
                            "category": {"type": "STRING"},
                            "unit": {"type": "STRING"},
                            "pack_size": {"type": "NUMBER"},
                        },
                    },
                },
                "required": ["raw_name"],
            },
        },
    },
    "required": ["lines"],
}

PARSE_SYSTEM_PROMPT = (
    "You extract line items from a supplier order or invoice document (any language). "
    "For each ordered product return quantity, unit price and line total. Then match "
    "it to ONE inventory item from the provided catalog and return that item's integer "
    "INDEX as matched_index (use -1 for no match). Do NOT return a sku or a name — only "
    "the index. MATCH DECISIVELY by PRODUCT TYPE + SIZE/VOLUME, across brand vs generic "
    "names and across languages: a branded document name matches a generic catalog name "
    "for the same product. The catalog is given in TWO sections. STRONGLY prefer an "
    "item from the SUPPLIER CATALOG section when it is the same product; only fall to "
    "OTHER INVENTORY if the supplier section has nothing suitable. Return "
    "matched_index=-1 ONLY when NOTHING in either section is plausibly the same product "
    "— do NOT refuse a match merely because the wording differs. If the KNOWN MAPPINGS "
    "section maps a document line-name to an index, that mapping is authoritative — use "
    "it. When matched_index is -1, fill suggested_new. In suggested_new, unit MUST be "
    "the BASE stocking unit — a singular consumable unit like 'bottle', 'can', 'pcs', "
    "'kg', 'g', 'l' — NOT a pack/case/box word. Set suggested_new.pack_size to the "
    "number of base units in ONE ordered pack ONLY when the document or the product "
    "name indicates it (e.g. '6-pack', 'case of 24'); otherwise leave it null — do NOT "
    "guess a pack size the document does not support. INCLUDE every ordered PRODUCT line "
    "— any row that has an order quantity and a line price. Only SKIP standalone "
    "SUMMARY/TOTAL rows that are not an ordered product: tax lines, discounts, and the "
    "order grand total. Return only JSON."
)


def normalize_name(value: Any) -> str:
    """Normalize a document line-name into an alias key.

    Lowercased, stripped, internal whitespace collapsed. Used by BOTH the alias write
    path and lookup so keys always agree.
    """
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip().lower())


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _parse_json(text: str) -> dict:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _fmt_catalog_item(idx: int, c: dict) -> str:
    """One indexed catalog line for the prompt."""
    return "[{}] {}".format(
        idx,
        " | ".join(
            str(x) if x is not None else ""
            for x in (
                c["name"],
                c["unit"],
                c["category"],
                c["supplier_sku"],
                c["pack_name"],
                c["pack_size"],
                c["price_per_pack"],
            )
        ),
    )


async def _extract_and_match(
    llm: LLMProvider,
    *,
    data: bytes,
    mime: str,
    catalog: list[dict],
    known_aliases: dict[str, str],
    models: Sequence[str | None],
) -> list[dict] | None:
    """Model step: extract lines + resolve each ``matched_index`` -> ``_resolved_sku``.

    Returns the raw line dicts (with ``_resolved_sku`` added), or ``None`` if the
    document was unreadable / every model attempt failed.
    """
    supplier_items = [c for c in catalog if c["from_supplier"]]
    other_items = [c for c in catalog if not c["from_supplier"]]
    ordered = supplier_items + other_items
    sku_by_index = {i: c["inventory_sku"] for i, c in enumerate(ordered)}
    index_by_sku = {c["inventory_sku"]: i for i, c in enumerate(ordered)}

    supplier_block = (
        "\n".join(_fmt_catalog_item(i, c) for i, c in enumerate(ordered) if c["from_supplier"])
        or "(none)"
    )
    other_block = (
        "\n".join(
            _fmt_catalog_item(i, c) for i, c in enumerate(ordered) if not c["from_supplier"]
        )
        or "(none)"
    )

    aliases_block = ""
    if known_aliases:
        hint_lines = "\n".join(
            f"{norm} => [{index_by_sku[sku]}]"
            for norm, sku in known_aliases.items()
            if sku in index_by_sku
        )
        if hint_lines:
            aliases_block = (
                "\n\nKNOWN MAPPINGS (authoritative document line-name => index; when a "
                "document line matches one of these by name, use that index):\n"
                f"{hint_lines}\n"
            )

    user_prompt = (
        "Each catalog item below is prefixed with [index] then: "
        "name | unit | category | supplier_sku | pack_name | pack_size | price_per_pack. "
        "Return the [index] number in matched_index (or -1).\n\n"
        "SUPPLIER CATALOG — items this supplier sells (STRONGLY prefer matching to these):\n"
        f"{supplier_block}\n\n"
        "OTHER INVENTORY:\n"
        f"{other_block}\n"
        f"{aliases_block}"
        "\nThe supplier order document is attached. Extract its product line items and "
        "match each to a catalog index as instructed."
    )
    image = ImagePart(data=data, mime_type=mime)

    result: dict | None = None
    for model_name in models:
        try:
            completion = await llm.complete(
                user_prompt,
                system_prompt=PARSE_SYSTEM_PROMPT,
                schema=PARSE_RESPONSE_SCHEMA,
                images=[image],
                model=model_name,
                temperature=0,
            )
        except AdapterError as exc:  # transient/permanent — try the next model
            logger.warning("order-import model %s failed: %s", model_name, exc)
            continue
        parsed = _parse_json(completion.text)
        if parsed:
            result = parsed
            break

    if result is None:
        return None

    raw_lines = result.get("lines")
    if not isinstance(raw_lines, list):
        return []
    for raw in raw_lines:
        if not isinstance(raw, dict):
            continue
        idx = raw.get("matched_index")
        raw["_resolved_sku"] = (
            sku_by_index.get(idx) if isinstance(idx, int) and idx >= 0 else None
        )
    return raw_lines


async def parse_order_document(
    llm: LLMProvider,
    session: AsyncSession,
    *,
    data: bytes,
    mime: str,
    supplier_id: str | None,
    models: Sequence[str | None] = (None,),
) -> dict | None:
    """Parse + match a supplier order document. ``None`` if unreadable (caller -> 502).

    Returns ``{"supplier_id": ..., "lines": [...]}``. Learned aliases deterministically
    override the model; matched lines are authoritatively enriched from the catalog.
    """
    sid = (supplier_id or "").strip() or None

    catalog = await crud.get_order_match_catalog(session, supplier_id=sid)
    catalog_by_sku = {c["inventory_sku"]: c for c in catalog}

    known_aliases: dict[str, str] = {}
    if sid:
        known_aliases = await crud.get_order_import_aliases(session, supplier_id=sid)

    raw_lines = await _extract_and_match(
        llm,
        data=data,
        mime=mime,
        catalog=catalog,
        known_aliases=known_aliases,
        models=models,
    )
    if raw_lines is None:
        return None

    lines: list[dict] = []
    for raw in raw_lines:
        if not isinstance(raw, dict):
            continue
        raw_name = str(raw.get("raw_name") or "").strip()
        norm = normalize_name(raw_name)

        alias_sku = known_aliases.get(norm) if norm else None
        match: dict | None
        if alias_sku and alias_sku in catalog_by_sku:
            match = catalog_by_sku[alias_sku]
            match_source: str | None = "alias"
        else:
            model_sku = raw.get("_resolved_sku")
            match = catalog_by_sku.get(model_sku) if isinstance(model_sku, str) else None
            match_source = "ai" if match is not None else None

        matched = match is not None

        suggested_new = None
        sn = raw.get("suggested_new")
        if not matched and isinstance(sn, dict) and (sn.get("name") or "").strip():
            # Free-text category — the core has no fixed taxonomy.
            cat = (sn.get("category") or "").strip() or "other"
            suggested_new = {
                "name": str(sn["name"]).strip(),
                "category": cat,
                "unit": (str(sn["unit"]).strip() if sn.get("unit") else None),
                "pack_size": _num(sn.get("pack_size")),
            }

        line: dict[str, Any] = {
            "raw_name": raw_name,
            "quantity": _num(raw.get("quantity")),
            "unit_price": _num(raw.get("unit_price")),
            "line_total": _num(raw.get("line_total")),
            "matched": matched,
            "match_confidence": (
                (1.0 if match_source == "alias" else (_num(raw.get("match_confidence")) or 0.0))
                if matched
                else 0.0
            ),
            "match_source": match_source,
            "inventory_sku": None,
            "inventory_name": None,
            "unit": None,
            "supplier_sku": None,
            "pack_name": None,
            "pack_size": None,
            "price_per_pack": None,
            "suggested_new": suggested_new,
        }

        if matched and match is not None:
            line["inventory_sku"] = match["inventory_sku"]
            line["inventory_name"] = match["name"]
            line["unit"] = match["unit"]
            line["supplier_sku"] = match["supplier_sku"]
            line["pack_name"] = match["pack_name"]
            line["pack_size"] = match["pack_size"]
            line["price_per_pack"] = match["price_per_pack"]
            line["suggested_new"] = None

        lines.append(line)

    return {"supplier_id": sid, "lines": lines}


__all__ = [
    "PARSE_RESPONSE_SCHEMA",
    "PARSE_SYSTEM_PROMPT",
    "normalize_name",
    "parse_order_document",
]
