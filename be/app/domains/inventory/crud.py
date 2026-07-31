"""Inventory domain CRUD — ``text()`` SQL constants + async functions.

Covers item definitions, per-venue stock, suppliers + supplier_items, purchase
orders + items, supplier monthly payments, and the order-import learned-alias memory.

All SQL is dialect-agnostic (sqlite in tests, postgres in prod), mirroring
``be.app.domains.menu.crud``:

* unqualified table names (no ``inventory_stg.`` prefix);
* surrogate ids allocated app-side (``SELECT COALESCE(MAX(id),0)+1``) — ``INTEGER
  PRIMARY KEY`` only autoincrements on sqlite;
* booleans stored as INTEGER 0/1; JSON list columns stored as TEXT and parsed here;
* money/quantity bound as text through ``CAST(:x AS numeric)`` (asyncpg bare-param
  arithmetic is otherwise ambiguous);
* ``created_at``/``updated_at`` written as ISO-8601 strings from Python (no
  ``now()``/``CURRENT_DATE``);
* upserts are select-then-insert/update (no postgres-only ``ON CONFLICT``);
* set membership uses an expanding IN bind (no postgres-only ``ANY(...)``).

Cross-domain ``inventory_sku`` references (recipes, aliases) stay SOFT — validated in
Python where it matters, never a hard FK.

VENUE SCOPING: ``stock`` / ``purchase_orders`` / ``purchase_items`` /
``supplier_monthly_payments`` are venue-partitioned; ``inventory_items`` /
``suppliers`` / ``supplier_items`` / ``order_import_aliases`` are company-level (they
carry ``venue_id`` for uniformity but are not filtered by it).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import RowMapping, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

VALID_LOCATIONS: tuple[str, ...] = ("storage", "kitchen", "walk_in", "foh")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _slug(value: str, default: str = "item") -> str:
    """Lowercase underscore-separated token, brand/locale-neutral (ASCII fallback)."""
    out = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return out or default


def _num_str(value: Any) -> str | None:
    """Bind a numeric as text for ``CAST(:x AS numeric)``; ``None`` stays ``None``."""
    return None if value is None else str(value)


def _to_bool(value: Any) -> bool:
    return bool(value)


def _dump_list(value: list[str] | None) -> str:
    return json.dumps(list(value or []), ensure_ascii=False)


def _load_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None if isinstance(value, bool) else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _next_id(session: AsyncSession, table: str) -> int:
    stmt = text(f"SELECT COALESCE(MAX(id), 0) + 1 AS n FROM {table}")  # noqa: S608 - fixed literal
    row = (await session.execute(stmt)).mappings().first()
    return int(row["n"]) if row is not None else 1


# --------------------------------------------------------------------------- #
# Inventory items
# --------------------------------------------------------------------------- #

_LIST_ITEMS = text("""
    SELECT sku, name, category, unit, is_active, reorder_point, max_threshold,
           comment, allowed_locations
    FROM inventory_items
    WHERE is_active = 1
    ORDER BY name
""")

_GET_ITEM = text("""
    SELECT sku, name, category, unit, is_active, reorder_point, max_threshold,
           comment, allowed_locations
    FROM inventory_items
    WHERE sku = :sku
""")

_INSERT_ITEM = text("""
    INSERT INTO inventory_items
        (sku, venue_id, name, category, unit, allowed_locations, reorder_point,
         max_threshold, comment, is_active, created_at, updated_at)
    VALUES
        (:sku, :venue_id, :name, :category, :unit, :allowed_locations,
         CAST(:reorder_point AS numeric), CAST(:max_threshold AS numeric),
         :comment, :is_active, :created_at, :updated_at)
""")

_UPDATE_ITEM = text("""
    UPDATE inventory_items
    SET name = :name, category = :category, unit = :unit,
        allowed_locations = :allowed_locations,
        reorder_point = CAST(:reorder_point AS numeric),
        max_threshold = CAST(:max_threshold AS numeric),
        comment = :comment, is_active = :is_active, updated_at = :updated_at
    WHERE sku = :sku
""")


def _row_to_item(m: RowMapping) -> dict:
    return {
        "sku": m["sku"],
        "name": m["name"],
        "category": m["category"],
        "unit": m["unit"],
        "is_active": _to_bool(m["is_active"]),
        "reorder_point": _num(m["reorder_point"]),
        "max_threshold": _num(m["max_threshold"]),
        "comment": m["comment"],
        "allowed_locations": _load_list(m["allowed_locations"]),
    }


async def list_items(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(_LIST_ITEMS)).mappings().all()
    return [_row_to_item(r) for r in rows]


async def get_item(session: AsyncSession, *, sku: str) -> dict | None:
    row = (await session.execute(_GET_ITEM, {"sku": sku})).mappings().first()
    return _row_to_item(row) if row is not None else None


async def create_item(
    session: AsyncSession,
    *,
    venue_id: str,
    sku: str | None,
    name: str,
    category: str,
    unit: str,
    allowed_locations: list[str],
    reorder_point: float | None,
    max_threshold: float | None,
    comment: str | None,
) -> dict:
    """Create an item definition. Raises ValueError on bad input / duplicate sku."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    locations = [str(loc) for loc in (allowed_locations or [])]
    if not locations:
        raise ValueError("at least one allowed location must be selected")
    invalid = sorted(set(locations) - set(VALID_LOCATIONS))
    if invalid:
        raise ValueError(f"invalid locations: {', '.join(invalid)}")

    new_sku = (sku or "").strip() or _slug(name)
    if await get_item(session, sku=new_sku) is not None:
        raise ValueError(f"item with sku '{new_sku}' already exists")

    now = _now_iso()
    await session.execute(
        _INSERT_ITEM,
        {
            "sku": new_sku,
            "venue_id": venue_id,
            "name": name,
            "category": (category or "").strip(),
            "unit": (unit or "").strip() or "units",
            "allowed_locations": _dump_list(locations),
            "reorder_point": _num_str(reorder_point),
            "max_threshold": _num_str(max_threshold),
            "comment": comment,
            "is_active": 1,
            "created_at": now,
            "updated_at": now,
        },
    )
    await session.commit()
    created = await get_item(session, sku=new_sku)
    assert created is not None
    return created


async def update_item(
    session: AsyncSession,
    *,
    sku: str,
    fields: dict[str, Any],
) -> dict | None:
    """Merge *fields* (only provided keys) into an item. ``None`` if the sku is absent.

    Read-merge-write (no dynamic SQL) keeps the statement portable and injection-safe.
    """
    current = await get_item(session, sku=sku)
    if current is None:
        return None

    if "allowed_locations" in fields and fields["allowed_locations"] is not None:
        locations = [str(loc) for loc in fields["allowed_locations"]]
        if not locations:
            raise ValueError("at least one allowed location must be selected")
        invalid = sorted(set(locations) - set(VALID_LOCATIONS))
        if invalid:
            raise ValueError(f"invalid locations: {', '.join(invalid)}")
        current["allowed_locations"] = locations

    for key in ("name", "category", "unit", "reorder_point", "max_threshold", "comment"):
        if key in fields and fields[key] is not None:
            current[key] = fields[key]
    if fields.get("is_active") is not None:
        current["is_active"] = bool(fields["is_active"])

    await session.execute(
        _UPDATE_ITEM,
        {
            "sku": sku,
            "name": current["name"],
            "category": current["category"],
            "unit": current["unit"],
            "allowed_locations": _dump_list(current["allowed_locations"]),
            "reorder_point": _num_str(current["reorder_point"]),
            "max_threshold": _num_str(current["max_threshold"]),
            "comment": current["comment"],
            "is_active": 1 if current["is_active"] else 0,
            "updated_at": _now_iso(),
        },
    )
    await session.commit()
    return await get_item(session, sku=sku)


# --------------------------------------------------------------------------- #
# Stock
# --------------------------------------------------------------------------- #

_GET_STOCK_ROW = text("""
    SELECT quantity FROM stock
    WHERE inventory_sku = :sku AND location = :location AND venue_id = :venue_id
""")

_INSERT_STOCK = text("""
    INSERT INTO stock (inventory_sku, location, venue_id, quantity, unit, updated_at)
    VALUES (:sku, :location, :venue_id, CAST(:quantity AS numeric), :unit, :updated_at)
""")

_UPDATE_STOCK = text("""
    UPDATE stock SET quantity = CAST(:quantity AS numeric), unit = :unit,
                     updated_at = :updated_at
    WHERE inventory_sku = :sku AND location = :location AND venue_id = :venue_id
""")

_STOCK_FOR_VENUE = text("""
    SELECT inventory_sku, location, quantity
    FROM stock
    WHERE venue_id = :venue_id
""")


async def _current_qty(
    session: AsyncSession, *, sku: str, location: str, venue_id: str
) -> float | None:
    row = (
        await session.execute(
            _GET_STOCK_ROW, {"sku": sku, "location": location, "venue_id": venue_id}
        )
    ).mappings().first()
    return _num(row["quantity"]) if row is not None else None


async def _write_qty(
    session: AsyncSession, *, sku: str, location: str, venue_id: str, quantity: float, unit: str
) -> None:
    """Insert-or-update the absolute quantity for one (sku, location, venue). No commit."""
    exists = await _current_qty(session, sku=sku, location=location, venue_id=venue_id)
    params = {
        "sku": sku,
        "location": location,
        "venue_id": venue_id,
        "quantity": str(quantity),
        "unit": unit,
        "updated_at": _now_iso(),
    }
    if exists is None:
        await session.execute(_INSERT_STOCK, params)
    else:
        await session.execute(_UPDATE_STOCK, params)


async def set_stock(
    session: AsyncSession, *, sku: str, location: str, venue_id: str, quantity: float
) -> dict:
    """Set the absolute quantity at one location. Raises ValueError on bad input."""
    if location not in VALID_LOCATIONS:
        raise ValueError(f"location must be one of {list(VALID_LOCATIONS)}")
    item = await get_item(session, sku=sku)
    if item is None:
        raise ValueError("sku not found")
    unit = item["unit"] or "units"
    await _write_qty(
        session, sku=sku, location=location, venue_id=venue_id, quantity=quantity, unit=unit
    )
    await session.commit()
    return {"sku": sku, "location": location, "quantity": quantity}


async def add_stock(
    session: AsyncSession,
    *,
    sku: str,
    location: str,
    venue_id: str,
    delta: float,
    unit: str,
    commit: bool = True,
) -> None:
    """Increment (or decrement) the quantity at one location. No item lookup here."""
    current = await _current_qty(session, sku=sku, location=location, venue_id=venue_id) or 0.0
    await _write_qty(
        session,
        sku=sku,
        location=location,
        venue_id=venue_id,
        quantity=current + delta,
        unit=unit,
    )
    if commit:
        await session.commit()


async def move_stock(
    session: AsyncSession,
    *,
    sku: str,
    from_location: str,
    to_location: str,
    venue_id: str,
    quantity: float,
) -> dict:
    """Atomically move *quantity* between two locations. Raises ValueError on bad input.

    Replaces the live FE's two-call non-atomic hack with a single-commit transfer.
    """
    if from_location not in VALID_LOCATIONS or to_location not in VALID_LOCATIONS:
        raise ValueError(f"locations must be within {list(VALID_LOCATIONS)}")
    if from_location == to_location:
        raise ValueError("from_location and to_location must differ")
    if quantity <= 0:
        raise ValueError("quantity must be > 0")
    item = await get_item(session, sku=sku)
    if item is None:
        raise ValueError("sku not found")
    unit = item["unit"] or "units"

    available = await _current_qty(session, sku=sku, location=from_location, venue_id=venue_id) or 0.0
    if available < quantity:
        raise ValueError("insufficient quantity at source location")

    await add_stock(
        session, sku=sku, location=from_location, venue_id=venue_id, delta=-quantity, unit=unit,
        commit=False,
    )
    await add_stock(
        session, sku=sku, location=to_location, venue_id=venue_id, delta=quantity, unit=unit,
        commit=False,
    )
    await session.commit()
    return {
        "sku": sku,
        "from_location": from_location,
        "to_location": to_location,
        "quantity": quantity,
    }


async def _stock_by_sku(session: AsyncSession, *, venue_id: str) -> dict[str, dict]:
    rows = (await session.execute(_STOCK_FOR_VENUE, {"venue_id": venue_id})).mappings().all()
    out: dict[str, dict] = {}
    for r in rows:
        sku = r["inventory_sku"]
        bucket = out.setdefault(
            sku, {"storage": 0.0, "kitchen": 0.0, "walk_in": 0.0, "foh": 0.0, "total": 0.0}
        )
        qty = _num(r["quantity"]) or 0.0
        bucket[r["location"]] = qty
        bucket["total"] += qty
    return out


# --------------------------------------------------------------------------- #
# Suppliers
# --------------------------------------------------------------------------- #

_LIST_SUPPLIERS = text("""
    SELECT supplier_id, name, contact_person, payment_type, site, chat_link, comment,
           categories, approx_delivery_time_info, is_active, created_at, updated_at,
           min_order_amount, payment_day, order_cutoff_time, delivery_lead_days,
           delivery_working_days_only
    FROM suppliers
    ORDER BY name
""")

_GET_SUPPLIER = text("SELECT created_at FROM suppliers WHERE supplier_id = :supplier_id")

_INSERT_SUPPLIER = text("""
    INSERT INTO suppliers
        (supplier_id, venue_id, name, contact_person, payment_type, site, chat_link,
         comment, categories, approx_delivery_time_info, is_active, min_order_amount,
         payment_day, order_cutoff_time, delivery_lead_days, delivery_working_days_only,
         created_at, updated_at)
    VALUES
        (:supplier_id, :venue_id, :name, :contact_person, :payment_type, :site,
         :chat_link, :comment, :categories, :approx_delivery_time_info, :is_active,
         CAST(:min_order_amount AS numeric), :payment_day, :order_cutoff_time,
         :delivery_lead_days, :delivery_working_days_only, :created_at, :updated_at)
""")

_UPDATE_SUPPLIER = text("""
    UPDATE suppliers SET
        name = :name, contact_person = :contact_person, payment_type = :payment_type,
        site = :site, chat_link = :chat_link, comment = :comment, categories = :categories,
        approx_delivery_time_info = :approx_delivery_time_info, is_active = :is_active,
        min_order_amount = CAST(:min_order_amount AS numeric), payment_day = :payment_day,
        order_cutoff_time = :order_cutoff_time, delivery_lead_days = :delivery_lead_days,
        delivery_working_days_only = :delivery_working_days_only, updated_at = :updated_at
    WHERE supplier_id = :supplier_id
""")


def _row_to_supplier(m: RowMapping) -> dict:
    return {
        "supplier_id": m["supplier_id"],
        "name": m["name"],
        "contact_person": m["contact_person"],
        "payment_type": m["payment_type"],
        "site": m["site"],
        "chat_link": m["chat_link"],
        "comment": m["comment"],
        "categories": _load_list(m["categories"]),
        "approx_delivery_time_info": m["approx_delivery_time_info"],
        "is_active": _to_bool(m["is_active"]),
        "created_at": m["created_at"],
        "updated_at": m["updated_at"],
        "min_order_amount": _num(m["min_order_amount"]),
        "payment_day": m["payment_day"],
        "order_cutoff_time": m["order_cutoff_time"],
        "delivery_lead_days": m["delivery_lead_days"],
        "delivery_working_days_only": _to_bool(m["delivery_working_days_only"]),
    }


async def list_suppliers(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(_LIST_SUPPLIERS)).mappings().all()
    return [_row_to_supplier(r) for r in rows]


async def upsert_supplier(
    session: AsyncSession,
    *,
    venue_id: str,
    supplier_id: str,
    name: str,
    contact_person: str | None,
    payment_type: str | None,
    site: str | None,
    chat_link: str | None,
    comment: str | None,
    categories: list[str] | None,
    approx_delivery_time_info: str | None,
    is_active: bool,
    min_order_amount: float | None,
    payment_day: int | None,
    order_cutoff_time: str | None,
    delivery_lead_days: int | None,
    delivery_working_days_only: bool,
) -> dict:
    """Insert-or-update a supplier (select-then-write; created_at preserved)."""
    now = _now_iso()
    existing = (
        await session.execute(_GET_SUPPLIER, {"supplier_id": supplier_id})
    ).mappings().first()
    params = {
        "supplier_id": supplier_id,
        "venue_id": venue_id,
        "name": name,
        "contact_person": contact_person,
        "payment_type": payment_type,
        "site": site,
        "chat_link": chat_link,
        "comment": comment,
        "categories": _dump_list(categories),
        "approx_delivery_time_info": approx_delivery_time_info,
        "is_active": 1 if is_active else 0,
        "min_order_amount": _num_str(min_order_amount),
        "payment_day": payment_day,
        # order_cutoff_time stored as TEXT "HH:MM" — no asyncpg TIME binding needed.
        "order_cutoff_time": order_cutoff_time,
        "delivery_lead_days": delivery_lead_days,
        "delivery_working_days_only": 1 if delivery_working_days_only else 0,
        "updated_at": now,
    }
    if existing is None:
        params["created_at"] = now
        await session.execute(_INSERT_SUPPLIER, params)
    else:
        await session.execute(_UPDATE_SUPPLIER, params)
    await session.commit()
    row = (await session.execute(_LIST_SUPPLIERS)).mappings().all()
    for m in row:
        if m["supplier_id"] == supplier_id:
            return _row_to_supplier(m)
    raise RuntimeError("supplier vanished after upsert")


# --------------------------------------------------------------------------- #
# Supplier items
# --------------------------------------------------------------------------- #

_GET_SUPPLIER_ITEM = text("""
    SELECT supplier_id, inventory_sku, supplier_sku, pack_name, pack_size,
           price_per_pack, tax_inclusive, is_primary_supplier, is_on_stop, link, comment
    FROM supplier_items
    WHERE inventory_sku = :inventory_sku AND supplier_id = :supplier_id
""")

_SUPPLIER_SKU_TAKEN = text("""
    SELECT si.inventory_sku, ii.name AS item_name
    FROM supplier_items si
    LEFT JOIN inventory_items ii ON ii.sku = si.inventory_sku
    WHERE si.supplier_id = :supplier_id
      AND si.supplier_sku = :supplier_sku
      AND si.inventory_sku <> :inventory_sku
""")

_INSERT_SUPPLIER_ITEM = text("""
    INSERT INTO supplier_items
        (supplier_id, inventory_sku, venue_id, supplier_sku, pack_name, pack_size,
         price_per_pack, tax_inclusive, is_primary_supplier, is_on_stop, link, comment,
         created_at, updated_at)
    VALUES
        (:supplier_id, :inventory_sku, :venue_id, :supplier_sku, :pack_name,
         CAST(:pack_size AS numeric), CAST(:price_per_pack AS numeric), :tax_inclusive,
         :is_primary_supplier, :is_on_stop, :link, :comment, :created_at, :updated_at)
""")

_UPDATE_SUPPLIER_ITEM = text("""
    UPDATE supplier_items SET
        supplier_sku = :supplier_sku, pack_name = :pack_name,
        pack_size = CAST(:pack_size AS numeric),
        price_per_pack = CAST(:price_per_pack AS numeric),
        tax_inclusive = :tax_inclusive, is_primary_supplier = :is_primary_supplier,
        is_on_stop = :is_on_stop, link = :link, comment = :comment, updated_at = :updated_at
    WHERE inventory_sku = :inventory_sku AND supplier_id = :supplier_id
""")


def _row_to_supplier_item(m: RowMapping) -> dict:
    return {
        "supplier_id": m["supplier_id"],
        "inventory_sku": m["inventory_sku"],
        "supplier_sku": m["supplier_sku"],
        "pack_name": m["pack_name"],
        "pack_size": _num(m["pack_size"]),
        "price_per_pack": _num(m["price_per_pack"]),
        "tax_inclusive": _to_bool(m["tax_inclusive"]),
        "is_primary_supplier": _to_bool(m["is_primary_supplier"]),
        "is_on_stop": _to_bool(m["is_on_stop"]),
        "link": m["link"],
        "comment": m["comment"],
    }


async def get_supplier_item(
    session: AsyncSession, *, inventory_sku: str, supplier_id: str
) -> dict | None:
    row = (
        await session.execute(
            _GET_SUPPLIER_ITEM, {"inventory_sku": inventory_sku, "supplier_id": supplier_id}
        )
    ).mappings().first()
    return _row_to_supplier_item(row) if row is not None else None


async def _supplier_sku_conflict(
    session: AsyncSession, *, supplier_id: str, supplier_sku: str, inventory_sku: str
) -> str | None:
    """Return the conflicting item name if *supplier_sku* is used by another item."""
    row = (
        await session.execute(
            _SUPPLIER_SKU_TAKEN,
            {
                "supplier_id": supplier_id,
                "supplier_sku": supplier_sku,
                "inventory_sku": inventory_sku,
            },
        )
    ).mappings().first()
    if row is None:
        return None
    return row["item_name"] or row["inventory_sku"]


def _clean(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


async def create_supplier_item(
    session: AsyncSession,
    *,
    venue_id: str,
    inventory_sku: str,
    supplier_id: str,
    supplier_sku: str | None,
    pack_name: str | None,
    pack_size: float | None,
    price_per_pack: float | None,
    tax_inclusive: bool,
    is_primary_supplier: bool,
    is_on_stop: bool,
    link: str | None,
    comment: str | None,
) -> dict:
    """Create a supplier_item. Raises ValueError (duplicate supplier_sku / already exists)."""
    if not supplier_id.strip():
        raise ValueError("supplier_id is required")
    final_sku = _clean(supplier_sku) or f"{supplier_id}_{inventory_sku}"

    if await get_supplier_item(session, inventory_sku=inventory_sku, supplier_id=supplier_id):
        raise ValueError("supplier item already exists for this inventory item")
    conflict = await _supplier_sku_conflict(
        session, supplier_id=supplier_id, supplier_sku=final_sku, inventory_sku=inventory_sku
    )
    if conflict:
        raise ValueError("this supplier SKU is already used for another item for the same supplier")

    now = _now_iso()
    await session.execute(
        _INSERT_SUPPLIER_ITEM,
        {
            "supplier_id": supplier_id,
            "inventory_sku": inventory_sku,
            "venue_id": venue_id,
            "supplier_sku": final_sku,
            "pack_name": _clean(pack_name),
            "pack_size": _num_str(pack_size),
            "price_per_pack": _num_str(price_per_pack),
            "tax_inclusive": 1 if tax_inclusive else 0,
            "is_primary_supplier": 1 if is_primary_supplier else 0,
            "is_on_stop": 1 if is_on_stop else 0,
            "link": _clean(link),
            "comment": _clean(comment),
            "created_at": now,
            "updated_at": now,
        },
    )
    await session.commit()
    created = await get_supplier_item(
        session, inventory_sku=inventory_sku, supplier_id=supplier_id
    )
    assert created is not None
    return created


async def update_supplier_item(
    session: AsyncSession,
    *,
    inventory_sku: str,
    supplier_id: str,
    fields: dict[str, Any],
) -> dict | None:
    """Merge *fields* into a supplier_item. ``None`` if absent; ValueError on conflict."""
    current = await get_supplier_item(
        session, inventory_sku=inventory_sku, supplier_id=supplier_id
    )
    if current is None:
        return None

    if "supplier_sku" in fields and fields["supplier_sku"] is not None:
        new_sku = _clean(fields["supplier_sku"]) or f"{supplier_id}_{inventory_sku}"
        conflict = await _supplier_sku_conflict(
            session, supplier_id=supplier_id, supplier_sku=new_sku, inventory_sku=inventory_sku
        )
        if conflict:
            raise ValueError(f"this SKU already exists in {conflict}.")
        current["supplier_sku"] = new_sku

    for key in ("pack_name", "link", "comment"):
        if key in fields and fields[key] is not None:
            current[key] = _clean(fields[key])
    for key in ("pack_size", "price_per_pack"):
        if key in fields and fields[key] is not None:
            current[key] = fields[key]
    for key in ("tax_inclusive", "is_primary_supplier", "is_on_stop"):
        if fields.get(key) is not None:
            current[key] = bool(fields[key])

    await session.execute(
        _UPDATE_SUPPLIER_ITEM,
        {
            "inventory_sku": inventory_sku,
            "supplier_id": supplier_id,
            "supplier_sku": current["supplier_sku"],
            "pack_name": current["pack_name"],
            "pack_size": _num_str(current["pack_size"]),
            "price_per_pack": _num_str(current["price_per_pack"]),
            "tax_inclusive": 1 if current["tax_inclusive"] else 0,
            "is_primary_supplier": 1 if current["is_primary_supplier"] else 0,
            "is_on_stop": 1 if current["is_on_stop"] else 0,
            "link": current["link"],
            "comment": current["comment"],
            "updated_at": _now_iso(),
        },
    )
    await session.commit()
    return await get_supplier_item(
        session, inventory_sku=inventory_sku, supplier_id=supplier_id
    )


# --------------------------------------------------------------------------- #
# Inventory page (merge)
# --------------------------------------------------------------------------- #

_SUPPLIERS_FOR_PAGE = text("""
    SELECT si.inventory_sku, si.supplier_id, s.name AS supplier_name, si.supplier_sku,
           si.pack_name, si.pack_size, si.price_per_pack, si.tax_inclusive,
           si.is_primary_supplier, si.is_on_stop
    FROM supplier_items si
    JOIN suppliers s ON s.supplier_id = si.supplier_id
    ORDER BY si.inventory_sku, si.is_primary_supplier DESC
""")

_EXPECTED_FOR_PAGE = text("""
    SELECT pi.inventory_sku, po.expected_delivery_date AS expected_date,
           pi.expected_quantity
    FROM purchase_items pi
    JOIN purchase_orders po ON po.order_id = pi.order_id AND po.venue_id = pi.venue_id
    WHERE po.status = 'placed' AND po.venue_id = :venue_id
""")


async def get_page_data(session: AsyncSession, *, venue_id: str) -> list[dict]:
    """Merge items + per-venue stock + supplier facts + expected deliveries."""
    items = await list_items(session)
    stock_by_sku = await _stock_by_sku(session, venue_id=venue_id)

    suppliers_by_sku: dict[str, list[dict]] = {}
    for r in (await session.execute(_SUPPLIERS_FOR_PAGE)).mappings().all():
        suppliers_by_sku.setdefault(r["inventory_sku"], []).append(
            {
                "supplier_id": r["supplier_id"],
                "supplier_name": r["supplier_name"],
                "supplier_sku": r["supplier_sku"],
                "pack_name": r["pack_name"],
                "pack_size": _num(r["pack_size"]),
                "price_per_pack": _num(r["price_per_pack"]),
                "tax_inclusive": _to_bool(r["tax_inclusive"]),
                "is_primary_supplier": _to_bool(r["is_primary_supplier"]),
                "is_on_stop": _to_bool(r["is_on_stop"]),
            }
        )

    expected_by_sku: dict[str, dict] = {}
    for r in (
        await session.execute(_EXPECTED_FOR_PAGE, {"venue_id": venue_id})
    ).mappings().all():
        sku = r["inventory_sku"]
        bucket = expected_by_sku.setdefault(sku, {"date": None, "quantity": 0.0})
        d = r["expected_date"]
        if d and (bucket["date"] is None or str(d) < str(bucket["date"])):
            bucket["date"] = str(d)
        bucket["quantity"] += _num(r["expected_quantity"]) or 0.0

    response: list[dict] = []
    for item in items:
        sku = item["sku"]
        response.append(
            {
                **item,
                "stock": stock_by_sku.get(
                    sku,
                    {"storage": 0.0, "kitchen": 0.0, "walk_in": 0.0, "foh": 0.0, "total": 0.0},
                ),
                "suppliers": suppliers_by_sku.get(sku, []),
                "expected": expected_by_sku.get(sku, {"date": None, "quantity": 0.0}),
            }
        )
    return response


# --------------------------------------------------------------------------- #
# Purchase orders
# --------------------------------------------------------------------------- #

_GET_ORDER = text("SELECT created_at FROM purchase_orders WHERE order_id = :order_id")

_INSERT_ORDER = text("""
    INSERT INTO purchase_orders
        (order_id, venue_id, date, supplier_id, supplier_name, status,
         expected_delivery_date, expected_total_amount, invoice_id, notes,
         created_at, placed_at)
    VALUES
        (:order_id, :venue_id, :date, :supplier_id, :supplier_name, :status,
         :expected_delivery_date, CAST(:expected_total_amount AS numeric), :invoice_id,
         :notes, :created_at, :placed_at)
""")

_UPDATE_ORDER = text("""
    UPDATE purchase_orders SET
        supplier_id = :supplier_id, supplier_name = :supplier_name, status = :status,
        placed_at = :placed_at, expected_delivery_date = :expected_delivery_date,
        expected_total_amount = CAST(:expected_total_amount AS numeric),
        invoice_id = :invoice_id, notes = :notes
    WHERE order_id = :order_id
""")

_DELETE_ORDER_ITEMS = text("DELETE FROM purchase_items WHERE order_id = :order_id")

_INSERT_ORDER_ITEM = text("""
    INSERT INTO purchase_items
        (id, venue_id, order_id, inventory_sku, date, packs, pack_size, pack_price,
         supplier_id, supplier_sku, is_received, expected_quantity, created_at)
    VALUES
        (:id, :venue_id, :order_id, :inventory_sku, :date, :packs,
         CAST(:pack_size AS numeric), CAST(:pack_price AS numeric), :supplier_id,
         :supplier_sku, 0, CAST(:expected_quantity AS numeric), :created_at)
""")

_MARK_ITEM_RECEIVED = text("""
    UPDATE purchase_items SET is_received = 1
    WHERE order_id = :order_id AND inventory_sku = :inventory_sku
""")

_RECEIVE_ORDER = text("""
    UPDATE purchase_orders SET status = 'received', received_at = :received_at
    WHERE order_id = :order_id
""")


async def upsert_purchase_order(
    session: AsyncSession,
    *,
    venue_id: str,
    order_id: str,
    supplier_id: str,
    supplier_name: str,
    status: str,
    expected_delivery_date: str | None,
    expected_total_amount: float | None,
    invoice_id: str | None,
    notes: str | None,
    items: list[dict],
) -> None:
    """Insert-or-update an order then delete-and-reinsert its items (expected_quantity
    computed app-side as packs*pack_size — GENERATED columns are not portable)."""
    now = _now_iso()
    today = now[:10]
    existing = (await session.execute(_GET_ORDER, {"order_id": order_id})).mappings().first()
    params = {
        "order_id": order_id,
        "venue_id": venue_id,
        "date": today,
        "supplier_id": supplier_id,
        "supplier_name": supplier_name,
        "status": status,
        "expected_delivery_date": expected_delivery_date,
        "expected_total_amount": _num_str(expected_total_amount),
        "invoice_id": invoice_id,
        "notes": notes,
        "placed_at": now,
    }
    if existing is None:
        params["created_at"] = now
        await session.execute(_INSERT_ORDER, params)
    else:
        await session.execute(_UPDATE_ORDER, params)

    await session.execute(_DELETE_ORDER_ITEMS, {"order_id": order_id})
    for item in items:
        packs = int(item.get("packs") or 0)
        pack_size = _num(item.get("pack_size"))
        expected_qty = packs * pack_size if pack_size is not None else None
        new_id = await _next_id(session, "purchase_items")
        await session.execute(
            _INSERT_ORDER_ITEM,
            {
                "id": new_id,
                "venue_id": venue_id,
                "order_id": order_id,
                "inventory_sku": item.get("inventory_sku"),
                "date": today,
                "packs": packs,
                "pack_size": _num_str(pack_size),
                "pack_price": _num_str(item.get("pack_price")),
                "supplier_id": item.get("supplier_id") or supplier_id,
                "supplier_sku": item.get("supplier_sku"),
                "expected_quantity": _num_str(expected_qty),
                "created_at": now,
            },
        )
    await session.commit()


async def receive_purchase_order(
    session: AsyncSession,
    *,
    venue_id: str,
    order_id: str,
    received_items: list[dict],
) -> None:
    """Mark an order received and add each received quantity to storage stock."""
    await session.execute(_RECEIVE_ORDER, {"order_id": order_id, "received_at": _now_iso()})
    for item in received_items:
        sku = item["inventory_sku"]
        quantity = float(item["quantity"])
        await session.execute(
            _MARK_ITEM_RECEIVED, {"order_id": order_id, "inventory_sku": sku}
        )
        db_item = await get_item(session, sku=sku)
        unit = (db_item["unit"] if db_item else None) or "units"
        await add_stock(
            session,
            sku=sku,
            location="storage",
            venue_id=venue_id,
            delta=quantity,
            unit=unit,
            commit=False,
        )
    await session.commit()


# --------------------------------------------------------------------------- #
# Order-import learned aliases + match catalog
# --------------------------------------------------------------------------- #

_EXISTING_SKUS = text(
    "SELECT sku FROM inventory_items WHERE sku IN :skus"
).bindparams(bindparam("skus", expanding=True))

_GET_ALIAS = text("""
    SELECT created_at FROM order_import_aliases
    WHERE supplier_id = :supplier_id AND raw_name_norm = :raw_name_norm
""")

_INSERT_ALIAS = text("""
    INSERT INTO order_import_aliases
        (supplier_id, raw_name_norm, venue_id, raw_name, inventory_sku, created_by,
         created_at, updated_at)
    VALUES
        (:supplier_id, :raw_name_norm, :venue_id, :raw_name, :inventory_sku,
         :created_by, :created_at, :updated_at)
""")

_UPDATE_ALIAS = text("""
    UPDATE order_import_aliases SET
        inventory_sku = :inventory_sku, raw_name = :raw_name, updated_at = :updated_at
    WHERE supplier_id = :supplier_id AND raw_name_norm = :raw_name_norm
""")

_GET_ALIASES_ALL = text("""
    SELECT a.raw_name_norm, a.inventory_sku
    FROM order_import_aliases a
    JOIN inventory_items ii ON ii.sku = a.inventory_sku AND ii.is_active = 1
    WHERE a.supplier_id = :supplier_id
""")

_MATCH_CATALOG_SUPPLIER = text("""
    SELECT ii.sku AS inventory_sku, ii.name AS name, ii.unit AS unit,
           ii.category AS category, si.supplier_sku AS supplier_sku,
           si.pack_name AS pack_name, si.pack_size AS pack_size,
           si.price_per_pack AS price_per_pack
    FROM supplier_items si
    JOIN inventory_items ii ON ii.sku = si.inventory_sku
    WHERE si.supplier_id = :supplier_id AND ii.is_active = 1
    ORDER BY ii.name
""")

_MATCH_CATALOG_ITEMS = text("""
    SELECT sku AS inventory_sku, name AS name, unit AS unit, category AS category
    FROM inventory_items
    WHERE is_active = 1
    ORDER BY name
""")


async def upsert_order_import_aliases(
    session: AsyncSession,
    *,
    venue_id: str,
    supplier_id: str,
    mappings: list[dict],
) -> int:
    """Bulk insert-or-update raw_name -> sku aliases; skips unknown skus. Returns count."""
    if not supplier_id or not mappings:
        return 0
    wanted = sorted({(m.get("inventory_sku") or "").strip() for m in mappings})
    wanted = [s for s in wanted if s]
    if not wanted:
        return 0
    existing_skus = set(
        (await session.execute(_EXISTING_SKUS, {"skus": wanted})).scalars().all()
    )

    written = 0
    for m in mappings:
        sku = (m.get("inventory_sku") or "").strip()
        raw_name = (m.get("raw_name") or "").strip()
        raw_name_norm = (m.get("raw_name_norm") or "").strip()
        if not sku or not raw_name or not raw_name_norm or sku not in existing_skus:
            continue
        now = _now_iso()
        found = (
            await session.execute(
                _GET_ALIAS, {"supplier_id": supplier_id, "raw_name_norm": raw_name_norm}
            )
        ).mappings().first()
        params = {
            "supplier_id": supplier_id,
            "raw_name_norm": raw_name_norm,
            "venue_id": venue_id,
            "raw_name": raw_name,
            "inventory_sku": sku,
            "created_by": m.get("created_by"),
            "updated_at": now,
        }
        if found is None:
            params["created_at"] = now
            await session.execute(_INSERT_ALIAS, params)
        else:
            await session.execute(_UPDATE_ALIAS, params)
        written += 1

    if written:
        await session.commit()
    return written


async def get_order_import_aliases(
    session: AsyncSession, *, supplier_id: str
) -> dict[str, str]:
    """Return ``{raw_name_norm: inventory_sku}`` for a supplier (active items only)."""
    if not supplier_id:
        return {}
    rows = (
        await session.execute(_GET_ALIASES_ALL, {"supplier_id": supplier_id})
    ).mappings().all()
    return {r["raw_name_norm"]: r["inventory_sku"] for r in rows}


async def get_order_match_catalog(
    session: AsyncSession, *, supplier_id: str | None
) -> list[dict]:
    """Match catalog for document-import: supplier items first, then all active items.

    De-duped by sku with the supplier row winning (it carries pack/price facts).
    """
    by_sku: dict[str, dict] = {}
    sid = (supplier_id or "").strip()
    if sid:
        for r in (
            await session.execute(_MATCH_CATALOG_SUPPLIER, {"supplier_id": sid})
        ).mappings().all():
            by_sku[r["inventory_sku"]] = {
                "inventory_sku": r["inventory_sku"],
                "name": r["name"],
                "unit": r["unit"],
                "category": r["category"],
                "supplier_sku": r["supplier_sku"],
                "pack_name": r["pack_name"],
                "pack_size": _num(r["pack_size"]),
                "price_per_pack": _num(r["price_per_pack"]),
                "from_supplier": True,
            }
    for r in (await session.execute(_MATCH_CATALOG_ITEMS)).mappings().all():
        sku = r["inventory_sku"]
        if sku in by_sku:
            continue
        by_sku[sku] = {
            "inventory_sku": sku,
            "name": r["name"],
            "unit": r["unit"],
            "category": r["category"],
            "supplier_sku": None,
            "pack_name": None,
            "pack_size": None,
            "price_per_pack": None,
            "from_supplier": False,
        }
    return list(by_sku.values())


# --------------------------------------------------------------------------- #
# Supplier monthly payments (minimal — full payments calendar is a later domain)
# --------------------------------------------------------------------------- #

_LIST_PAYMENTS = text("""
    SELECT id, supplier_id, period_year, period_month, status, paid_at, paid_amount,
           paid_method, proof_url, notes, created_at, updated_at
    FROM supplier_monthly_payments
    WHERE venue_id = :venue_id AND supplier_id = :supplier_id
    ORDER BY period_year DESC, period_month DESC
""")

_GET_PAYMENT = text("""
    SELECT id, created_at FROM supplier_monthly_payments
    WHERE venue_id = :venue_id AND supplier_id = :supplier_id
      AND period_year = :period_year AND period_month = :period_month
""")

_INSERT_PAYMENT = text("""
    INSERT INTO supplier_monthly_payments
        (id, venue_id, supplier_id, period_year, period_month, status, paid_at,
         paid_amount, paid_method, proof_url, notes, created_at, updated_at)
    VALUES
        (:id, :venue_id, :supplier_id, :period_year, :period_month, :status, :paid_at,
         CAST(:paid_amount AS numeric), :paid_method, :proof_url, :notes, :created_at,
         :updated_at)
""")

_UPDATE_PAYMENT = text("""
    UPDATE supplier_monthly_payments SET
        status = :status, paid_at = :paid_at,
        paid_amount = CAST(:paid_amount AS numeric), paid_method = :paid_method,
        proof_url = :proof_url, notes = :notes, updated_at = :updated_at
    WHERE id = :id
""")


def _row_to_payment(m: RowMapping) -> dict:
    return {
        "id": int(m["id"]),
        "supplier_id": m["supplier_id"],
        "period_year": m["period_year"],
        "period_month": m["period_month"],
        "status": m["status"],
        "paid_at": m["paid_at"],
        "paid_amount": _num(m["paid_amount"]),
        "paid_method": m["paid_method"],
        "proof_url": m["proof_url"],
        "notes": m["notes"],
        "created_at": m["created_at"],
        "updated_at": m["updated_at"],
    }


async def list_supplier_monthly_payments(
    session: AsyncSession, *, venue_id: str, supplier_id: str
) -> list[dict]:
    rows = (
        await session.execute(
            _LIST_PAYMENTS, {"venue_id": venue_id, "supplier_id": supplier_id}
        )
    ).mappings().all()
    return [_row_to_payment(r) for r in rows]


async def upsert_supplier_monthly_payment(
    session: AsyncSession,
    *,
    venue_id: str,
    supplier_id: str,
    period_year: int,
    period_month: int,
    status: str,
    paid_amount: float | None,
    paid_method: str | None,
    proof_url: str | None,
    notes: str | None,
) -> dict:
    """Insert-or-update one supplier monthly payment period. Raises ValueError on bad input."""
    if not supplier_id.strip():
        raise ValueError("supplier_id is required")
    if status not in ("pending", "paid"):
        raise ValueError("status must be 'pending' or 'paid'")
    if not (1 <= int(period_month) <= 12):
        raise ValueError("period_month must be between 1 and 12")
    now = _now_iso()
    paid_at = now if status == "paid" else None
    existing = (
        await session.execute(
            _GET_PAYMENT,
            {
                "venue_id": venue_id,
                "supplier_id": supplier_id,
                "period_year": period_year,
                "period_month": period_month,
            },
        )
    ).mappings().first()
    if existing is None:
        new_id = await _next_id(session, "supplier_monthly_payments")
        await session.execute(
            _INSERT_PAYMENT,
            {
                "id": new_id,
                "venue_id": venue_id,
                "supplier_id": supplier_id,
                "period_year": period_year,
                "period_month": period_month,
                "status": status,
                "paid_at": paid_at,
                "paid_amount": _num_str(paid_amount),
                "paid_method": paid_method,
                "proof_url": proof_url,
                "notes": notes,
                "created_at": now,
                "updated_at": now,
            },
        )
        row_id = new_id
    else:
        row_id = int(existing["id"])
        await session.execute(
            _UPDATE_PAYMENT,
            {
                "id": row_id,
                "status": status,
                "paid_at": paid_at,
                "paid_amount": _num_str(paid_amount),
                "paid_method": paid_method,
                "proof_url": proof_url,
                "notes": notes,
                "updated_at": now,
            },
        )
    await session.commit()
    payments = await list_supplier_monthly_payments(
        session, venue_id=venue_id, supplier_id=supplier_id
    )
    for p in payments:
        if p["id"] == row_id:
            return p
    raise RuntimeError("payment vanished after upsert")


__all__ = [
    "VALID_LOCATIONS",
    "list_items",
    "get_item",
    "create_item",
    "update_item",
    "set_stock",
    "add_stock",
    "move_stock",
    "get_page_data",
    "list_suppliers",
    "upsert_supplier",
    "get_supplier_item",
    "create_supplier_item",
    "update_supplier_item",
    "upsert_purchase_order",
    "receive_purchase_order",
    "upsert_order_import_aliases",
    "get_order_import_aliases",
    "get_order_match_catalog",
    "list_supplier_monthly_payments",
    "upsert_supplier_monthly_payment",
]
