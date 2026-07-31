"""Menu domain CRUD — ``text()`` SQL constants + async functions.

Two stores:

* **menu_item_versions** — append-only full-item content history with a single
  ``is_current`` row per ``(item_id, venue_id)``. Save clears the current flag and
  appends a new current row; restore appends a *new* current row copying an older
  snapshot (history stays append-only). The first tracked edit optionally records a
  revertable baseline of the externally-published state (via the projection seam).
* **menu_item_recipes** — menu-item(/option) -> inventory-sku soft links used by the
  order-finalize inventory drawdown (that consumer is out of scope; the read-side
  :func:`get_recipe_map_for_finalize` is kept for it).

All SQL is dialect-agnostic (sqlite in tests, postgres in prod): unqualified table
names, JSON snapshot bound as TEXT, ``is_current`` as INTEGER 0/1, quantity bound as
text through ``CAST(:q AS numeric)``, surrogate ids allocated app-side (portable —
``INTEGER PRIMARY KEY`` autoincrements only on sqlite), and ``created_at`` written as
an ISO-8601 string from Python.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import RowMapping, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

# Stable content fields a version snapshot may carry. Availability flags
# (item-level active/takeaway and per-option active) and the doc id are intentionally
# excluded — versions store *content* only; live availability lives in the read copy.
CONTENT_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "ingredients",
    "price",
    "size",
    "type",
    "category",
    "categories",
    "sortOrder",
    "image",
    "options",
    "showOnCookKds",
)

VALID_LOCATIONS = {"storage", "kitchen", "walk_in", "foh"}


# --------------------------------------------------------------------------- #
# SQL constants
# --------------------------------------------------------------------------- #

_NEXT_VERSION_ID = text("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM menu_item_versions")

_INSERT_VERSION = text("""
    INSERT INTO menu_item_versions
        (id, venue_id, item_id, snapshot, changed_by, changed_by_name,
         source_version_id, is_current, created_at)
    VALUES
        (:id, :venue_id, :item_id, :snapshot, :changed_by, :changed_by_name,
         :source_version_id, :is_current, :created_at)
    RETURNING id, venue_id, item_id, snapshot, changed_by, changed_by_name,
              source_version_id, is_current, created_at
""")

_CLEAR_CURRENT = text(
    "UPDATE menu_item_versions SET is_current = 0 "
    "WHERE item_id = :item_id AND venue_id = :venue_id AND is_current = 1"
)

_COUNT_FOR_ITEM = text(
    "SELECT count(*) AS n FROM menu_item_versions "
    "WHERE item_id = :item_id AND venue_id = :venue_id"
)

_LIST_VERSIONS = text("""
    SELECT id, venue_id, item_id, snapshot, changed_by, changed_by_name,
           source_version_id, is_current, created_at
    FROM menu_item_versions
    WHERE item_id = :item_id AND venue_id = :venue_id
    ORDER BY id DESC
""")

_GET_VERSION = text("""
    SELECT id, venue_id, item_id, snapshot
    FROM menu_item_versions
    WHERE id = :version_id AND item_id = :item_id AND venue_id = :venue_id
""")

_NEXT_RECIPE_ID = text("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM menu_item_recipes")

_SELECT_RECIPE = text("""
    SELECT menu_item_id, option_index, inventory_sku, location, quantity
    FROM menu_item_recipes
    WHERE menu_item_id = :menu_item_id AND venue_id = :venue_id
    ORDER BY option_index, inventory_sku
""")

_DELETE_RECIPE = text(
    "DELETE FROM menu_item_recipes "
    "WHERE menu_item_id = :menu_item_id AND venue_id = :venue_id"
)

_INSERT_LINE = text("""
    INSERT INTO menu_item_recipes
        (id, venue_id, menu_item_id, option_index, inventory_sku, location,
         quantity, updated_by, created_at, updated_at)
    VALUES
        (:id, :venue_id, :menu_item_id, :option_index, :inventory_sku, :location,
         CAST(:quantity AS numeric), :updated_by, :created_at, :updated_at)
""")

# Bulk lookup for order-finalize (leg #3). Expanding IN bind is portable across
# sqlite and postgres (avoids postgres-only ANY(:ids)).
_SELECT_RECIPE_MAP = text("""
    SELECT menu_item_id, option_index, inventory_sku, location, quantity
    FROM menu_item_recipes
    WHERE venue_id = :venue_id AND menu_item_id IN :ids
""").bindparams(bindparam("ids", expanding=True))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def clean_snapshot(raw: dict | None) -> dict:
    """Keep only stable content fields; drop the doc id and every availability flag.

    Per-option ``active`` is stripped so a restore never forces an option live/hidden.
    """
    if not isinstance(raw, dict):
        return {}
    snap: dict[str, Any] = {k: raw[k] for k in CONTENT_FIELDS if k in raw}
    options = snap.get("options")
    if isinstance(options, list):
        snap["options"] = [
            {k: v for k, v in opt.items() if k != "active"} if isinstance(opt, dict) else opt
            for opt in options
        ]
    return snap


def has_content(snap: dict) -> bool:
    """True if a snapshot carries any content field (used to skip empty baselines)."""
    return any(snap.get(k) for k in CONTENT_FIELDS)


def _decode_snapshot(value: Any) -> dict:
    """Snapshot column is TEXT (portable). Parse to dict; tolerate a native dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _row_to_version(m: RowMapping) -> dict:
    return {
        "id": int(m["id"]),
        "venue_id": m["venue_id"],
        "item_id": m["item_id"],
        "snapshot": _decode_snapshot(m["snapshot"]),
        "changed_by": m["changed_by"],
        "changed_by_name": m["changed_by_name"],
        "source_version_id": m["source_version_id"],
        "is_current": bool(m["is_current"]),
        "created_at": m["created_at"],
    }


def _line_to_dict(m: RowMapping) -> dict:
    qty = m["quantity"]
    return {
        "option_index": int(m["option_index"]),
        "inventory_sku": m["inventory_sku"],
        "location": m["location"],
        "quantity": float(qty) if qty is not None else 0.0,
    }


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #


async def _next_id(session: AsyncSession, stmt: Any) -> int:
    row = (await session.execute(stmt)).mappings().first()
    return int(row["n"]) if row is not None else 1


async def _insert_version(
    session: AsyncSession,
    *,
    venue_id: str,
    item_id: str,
    snapshot: dict,
    changed_by: str | None,
    changed_by_name: str | None,
    source_version_id: int | None,
    is_current: int,
) -> dict:
    new_id = await _next_id(session, _NEXT_VERSION_ID)
    row = (
        await session.execute(
            _INSERT_VERSION,
            {
                "id": new_id,
                "venue_id": venue_id,
                "item_id": item_id,
                "snapshot": json.dumps(snapshot, ensure_ascii=False),
                "changed_by": changed_by,
                "changed_by_name": changed_by_name,
                "source_version_id": source_version_id,
                "is_current": is_current,
                "created_at": _now_iso(),
            },
        )
    ).mappings().first()
    assert row is not None  # RETURNING always yields the inserted row
    return _row_to_version(row)


async def save_version(
    session: AsyncSession,
    *,
    item_id: str,
    venue_id: str,
    snapshot: dict,
    changed_by: str | None,
    changed_by_name: str | None = None,
    baseline_snapshot: dict | None = None,
) -> dict:
    """Record a new current full-item version. Returns the new current version.

    On the first tracked edit, if *baseline_snapshot* (the externally-published
    state, from the projection seam) has content, it is recorded first as a
    non-current revertable baseline so the very first edit can be undone.
    """
    cleaned = clean_snapshot(snapshot)

    count_row = (
        await session.execute(_COUNT_FOR_ITEM, {"item_id": item_id, "venue_id": venue_id})
    ).mappings().first()
    count = int(count_row["n"]) if count_row is not None else 0
    if count == 0:
        baseline = clean_snapshot(baseline_snapshot)
        if has_content(baseline):
            await _insert_version(
                session,
                venue_id=venue_id,
                item_id=item_id,
                snapshot=baseline,
                changed_by="(initial)",
                changed_by_name=None,
                source_version_id=None,
                is_current=0,
            )

    await session.execute(_CLEAR_CURRENT, {"item_id": item_id, "venue_id": venue_id})
    result = await _insert_version(
        session,
        venue_id=venue_id,
        item_id=item_id,
        snapshot=cleaned,
        changed_by=changed_by,
        changed_by_name=changed_by_name,
        source_version_id=None,
        is_current=1,
    )
    await session.commit()
    return result


async def list_versions(session: AsyncSession, *, item_id: str, venue_id: str) -> list[dict]:
    """Full edit history for an item (newest first). Current row has is_current=True."""
    rows = (
        await session.execute(_LIST_VERSIONS, {"item_id": item_id, "venue_id": venue_id})
    ).mappings().all()
    return [_row_to_version(r) for r in rows]


async def restore_version(
    session: AsyncSession,
    *,
    item_id: str,
    venue_id: str,
    version_id: int,
    changed_by: str | None,
    changed_by_name: str | None = None,
) -> dict | None:
    """Append a new current version copying *version_id*'s snapshot; ``None`` if absent."""
    target = (
        await session.execute(
            _GET_VERSION,
            {"version_id": version_id, "item_id": item_id, "venue_id": venue_id},
        )
    ).mappings().first()
    if target is None:
        return None
    snapshot = clean_snapshot(_decode_snapshot(target["snapshot"]))

    await session.execute(_CLEAR_CURRENT, {"item_id": item_id, "venue_id": venue_id})
    result = await _insert_version(
        session,
        venue_id=venue_id,
        item_id=item_id,
        snapshot=snapshot,
        changed_by=changed_by,
        changed_by_name=changed_by_name,
        source_version_id=version_id,
        is_current=1,
    )
    await session.commit()
    return result


# --------------------------------------------------------------------------- #
# Recipes
# --------------------------------------------------------------------------- #


def _validate_line(line: dict) -> dict:
    """Validate + coerce one incoming recipe line. Raises ValueError on bad input."""
    if not isinstance(line, dict):
        raise ValueError("Each recipe line must be an object.")

    sku = (line.get("inventory_sku") or "").strip()
    if not sku:
        raise ValueError("inventory_sku is required and must be non-empty.")

    location = (line.get("location") or "").strip()
    if location not in VALID_LOCATIONS:
        raise ValueError(
            f"location must be one of {sorted(VALID_LOCATIONS)}, got {location!r}."
        )

    raw_opt = line.get("option_index", -1)
    if raw_opt is None:
        raw_opt = -1
    try:
        option_index = int(raw_opt)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"option_index must be an integer, got {raw_opt!r}.") from exc

    raw_qty = line.get("quantity")
    try:
        quantity = Decimal(str(raw_qty))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError(f"quantity must be a number, got {raw_qty!r}.") from exc
    if quantity <= 0:
        raise ValueError(f"quantity must be > 0, got {quantity}.")

    return {
        "option_index": option_index,
        "inventory_sku": sku,
        "location": location,
        "quantity": quantity,
    }


async def get_recipe_for_item(
    session: AsyncSession, *, menu_item_id: str, venue_id: str
) -> list[dict]:
    """All recipe lines for one item, ordered by option_index, inventory_sku."""
    rows = (
        await session.execute(
            _SELECT_RECIPE, {"menu_item_id": menu_item_id, "venue_id": venue_id}
        )
    ).mappings().all()
    return [_line_to_dict(r) for r in rows]


async def replace_recipe_for_item(
    session: AsyncSession,
    *,
    menu_item_id: str,
    venue_id: str,
    updated_by: str | None,
    lines: list[dict],
) -> list[dict]:
    """Delete-then-insert the full recipe for one item in one commit.

    Validates every line before touching the DB (raises ValueError so the endpoint
    can 400). Returns the saved lines in canonical order.
    """
    validated = [_validate_line(line) for line in (lines or [])]

    await session.execute(_DELETE_RECIPE, {"menu_item_id": menu_item_id, "venue_id": venue_id})
    for line in validated:
        new_id = await _next_id(session, _NEXT_RECIPE_ID)
        now = _now_iso()
        await session.execute(
            _INSERT_LINE,
            {
                "id": new_id,
                "venue_id": venue_id,
                "menu_item_id": menu_item_id,
                "option_index": line["option_index"],
                "inventory_sku": line["inventory_sku"],
                "location": line["location"],
                "quantity": str(line["quantity"]),
                "updated_by": updated_by,
                "created_at": now,
                "updated_at": now,
            },
        )
    await session.commit()
    return await get_recipe_for_item(session, menu_item_id=menu_item_id, venue_id=venue_id)


async def get_recipe_map_for_finalize(
    session: AsyncSession, *, item_ids: list[str], venue_id: str
) -> dict[tuple[str, int], list[dict]]:
    """Bulk-load recipes for a set of item ids in ONE query (order-finalize leg #3).

    Returns ``{(menu_item_id, option_index): [{inventory_sku, location, quantity}, ...]}``.
    """
    result: dict[tuple[str, int], list[dict]] = {}
    ids = [i for i in dict.fromkeys(item_ids) if i]  # dedupe, drop falsy
    if not ids:
        return result
    rows = (
        await session.execute(_SELECT_RECIPE_MAP, {"ids": ids, "venue_id": venue_id})
    ).mappings().all()
    for m in rows:
        key = (m["menu_item_id"], int(m["option_index"]))
        qty = m["quantity"]
        result.setdefault(key, []).append(
            {
                "inventory_sku": m["inventory_sku"],
                "location": m["location"],
                "quantity": float(qty) if qty is not None else 0.0,
            }
        )
    return result


__all__ = [
    "CONTENT_FIELDS",
    "VALID_LOCATIONS",
    "clean_snapshot",
    "has_content",
    "save_version",
    "list_versions",
    "restore_version",
    "get_recipe_for_item",
    "replace_recipe_for_item",
    "get_recipe_map_for_finalize",
]
