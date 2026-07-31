"""Inventory / suppliers / purchasing admin router — thin endpoints via the seams.

Every route is admin-gated through the :class:`IdentityProvider` seam (Bearer token ->
:class:`Principal`, ``admin`` claim required). Stock and purchasing are venue-scoped
(``VenueDep``); item/supplier definitions and learned aliases are company-level. The
order-import document parse routes through the injected :class:`LLMProvider` only (no
vendor imports). The read-copy projection is a no-op seam (Firestore sync deferred).
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from be.adapters.llm.base import LLMProvider
from be.app.deps import AdminDep, DbDep, VenueDep, get_llm
from be.app.domains.inventory import crud, service
from be.app.domains.inventory.projection import (
    InventoryProjection,
    NullInventoryProjection,
)
from be.app.domains.inventory.schemas import (
    ImportAliasesPayload,
    InventoryItemCreateRequest,
    InventoryItemUpdateRequest,
    PlaceOrderPayload,
    ReceiveOrderPayload,
    StockMoveRequest,
    StockUpdateRequest,
    SupplierItemCreateRequest,
    SupplierItemUpdateRequest,
    SupplierMonthlyPaymentUpsert,
    SupplierUpsertRequest,
)

router = APIRouter(tags=["inventory"])

MAX_UPLOAD_BYTES = 5_000_000
ALLOWED_DOC_MIMES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
}


def get_inventory_projection() -> InventoryProjection:
    """Read-copy projection seam. No-op until a document-store adapter is wired."""
    return NullInventoryProjection()


LLMDep = Annotated[LLMProvider, Depends(get_llm)]
InventoryProjectionDep = Annotated[InventoryProjection, Depends(get_inventory_projection)]


def _slug(value: str, default: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return out or default


# --- inventory items --------------------------------------------------------


@router.get("/inventory/page")
async def inventory_page(_admin: AdminDep, venue_id: VenueDep, session: DbDep) -> list[dict]:
    """Merged inventory page: items + per-venue stock + supplier facts + expected."""
    return await crud.get_page_data(session, venue_id=venue_id)


@router.post("/inventory/items")
async def create_inventory_item(
    payload: InventoryItemCreateRequest,
    _admin: AdminDep,
    venue_id: VenueDep,
    session: DbDep,
) -> dict:
    try:
        return await crud.create_item(
            session,
            venue_id=venue_id,
            sku=payload.sku,
            name=payload.name,
            category=payload.category,
            unit=payload.unit,
            allowed_locations=payload.allowed_locations,
            reorder_point=payload.reorder_point,
            max_threshold=payload.max_threshold,
            comment=payload.comment,
        )
    except ValueError as exc:
        detail = str(exc)
        status = 409 if "already exists" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc


@router.put("/inventory/items/{sku}")
async def update_inventory_item(
    sku: str,
    payload: InventoryItemUpdateRequest,
    _admin: AdminDep,
    session: DbDep,
) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        updated = await crud.update_item(session, sku=sku, fields=fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="sku not found")
    return updated


# --- stock ------------------------------------------------------------------


@router.post("/inventory/stock/update")
async def update_stock(
    payload: StockUpdateRequest,
    _admin: AdminDep,
    venue_id: VenueDep,
    session: DbDep,
) -> dict:
    try:
        return await crud.set_stock(
            session,
            sku=payload.sku,
            location=payload.location,
            venue_id=venue_id,
            quantity=payload.quantity,
        )
    except ValueError as exc:
        detail = str(exc)
        status = 404 if detail == "sku not found" else 400
        raise HTTPException(status_code=status, detail=detail) from exc


@router.post("/inventory/stock/move")
async def move_stock(
    payload: StockMoveRequest,
    _admin: AdminDep,
    venue_id: VenueDep,
    session: DbDep,
) -> dict:
    """Atomically move stock between two locations (single commit)."""
    try:
        return await crud.move_stock(
            session,
            sku=payload.sku,
            from_location=payload.from_location,
            to_location=payload.to_location,
            venue_id=venue_id,
            quantity=payload.quantity,
        )
    except ValueError as exc:
        detail = str(exc)
        status = 404 if detail == "sku not found" else 400
        raise HTTPException(status_code=status, detail=detail) from exc


# --- supplier items ---------------------------------------------------------


@router.post("/inventory/supplier-items/{inventory_sku}")
async def create_supplier_item(
    inventory_sku: str,
    payload: SupplierItemCreateRequest,
    _admin: AdminDep,
    venue_id: VenueDep,
    session: DbDep,
) -> dict:
    try:
        return await crud.create_supplier_item(
            session,
            venue_id=venue_id,
            inventory_sku=inventory_sku,
            supplier_id=payload.supplier_id,
            supplier_sku=payload.supplier_sku,
            pack_name=payload.pack_name,
            pack_size=payload.pack_size,
            price_per_pack=payload.price_per_pack,
            tax_inclusive=payload.tax_inclusive,
            is_primary_supplier=payload.is_primary_supplier,
            is_on_stop=payload.is_on_stop,
            link=payload.link,
            comment=payload.comment,
        )
    except ValueError as exc:
        detail = str(exc)
        status = 409 if "already" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc


@router.put("/inventory/supplier-items/{inventory_sku}/{supplier_id}")
async def update_supplier_item(
    inventory_sku: str,
    supplier_id: str,
    payload: SupplierItemUpdateRequest,
    _admin: AdminDep,
    session: DbDep,
) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        updated = await crud.update_supplier_item(
            session, inventory_sku=inventory_sku, supplier_id=supplier_id, fields=fields
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="supplier item not found")
    return updated


# --- suppliers --------------------------------------------------------------


@router.get("/suppliers")
async def list_suppliers(_admin: AdminDep, session: DbDep) -> list[dict]:
    return await crud.list_suppliers(session)


@router.post("/suppliers")
async def upsert_supplier(
    payload: SupplierUpsertRequest,
    _admin: AdminDep,
    venue_id: VenueDep,
    session: DbDep,
) -> dict:
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    supplier_id = (payload.supplier_id or "").strip() or _slug(name, "supplier")

    if payload.payment_day is not None and not (1 <= payload.payment_day <= 31):
        raise HTTPException(status_code=400, detail="payment_day must be between 1 and 31")

    cutoff = (payload.order_cutoff_time or "").strip() or None
    if cutoff is not None and not re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", cutoff):
        raise HTTPException(status_code=400, detail="order_cutoff_time must be HH:MM")
    if cutoff is not None:  # normalize to HH:MM
        h, m = cutoff.split(":")[0], cutoff.split(":")[1]
        cutoff = f"{int(h):02d}:{int(m):02d}"

    if payload.delivery_lead_days is not None and payload.delivery_lead_days < 0:
        raise HTTPException(status_code=400, detail="delivery_lead_days must be >= 0")

    return await crud.upsert_supplier(
        session,
        venue_id=venue_id,
        supplier_id=supplier_id,
        name=name,
        contact_person=payload.contact_person,
        payment_type=payload.payment_type,
        site=payload.site,
        chat_link=payload.chat_link,
        comment=payload.comment,
        categories=payload.categories,
        approx_delivery_time_info=payload.approx_delivery_time_info,
        is_active=payload.is_active,
        min_order_amount=payload.min_order_amount,
        payment_day=payload.payment_day,
        order_cutoff_time=cutoff,
        delivery_lead_days=payload.delivery_lead_days,
        delivery_working_days_only=payload.delivery_working_days_only,
    )


# --- supplier monthly payments (minimal) ------------------------------------


@router.get("/suppliers/{supplier_id}/payments")
async def list_supplier_payments(
    supplier_id: str, _admin: AdminDep, venue_id: VenueDep, session: DbDep
) -> list[dict]:
    return await crud.list_supplier_monthly_payments(
        session, venue_id=venue_id, supplier_id=supplier_id
    )


@router.post("/suppliers/{supplier_id}/payments")
async def upsert_supplier_payment(
    supplier_id: str,
    payload: SupplierMonthlyPaymentUpsert,
    _admin: AdminDep,
    venue_id: VenueDep,
    session: DbDep,
) -> dict:
    sid = (payload.supplier_id or "").strip() or supplier_id
    try:
        return await crud.upsert_supplier_monthly_payment(
            session,
            venue_id=venue_id,
            supplier_id=sid,
            period_year=payload.period_year,
            period_month=payload.period_month,
            status=payload.status,
            paid_amount=payload.paid_amount,
            paid_method=payload.paid_method,
            proof_url=payload.proof_url,
            notes=payload.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- purchasing -------------------------------------------------------------


@router.post("/orders/placed")
async def place_order(
    payload: PlaceOrderPayload,
    _admin: AdminDep,
    venue_id: VenueDep,
    session: DbDep,
    projection: InventoryProjectionDep,
) -> dict:
    if not payload.order_id:
        raise HTTPException(status_code=400, detail="order_id is required")
    if not payload.supplier_id:
        raise HTTPException(status_code=400, detail="supplier_id is required")
    invalid = [
        it.inventory_sku for it in payload.items if not it.pack_size or it.pack_size <= 0
    ]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"invalid pack size for items: {', '.join(invalid)}. Must be > 0.",
        )

    items = [
        {
            "inventory_sku": it.inventory_sku,
            "packs": it.packs,
            "pack_size": it.pack_size,
            "pack_price": it.pack_price,
            "supplier_id": it.supplier_id or payload.supplier_id,
            "supplier_sku": it.supplier_sku,
        }
        for it in payload.items
    ]
    await crud.upsert_purchase_order(
        session,
        venue_id=venue_id,
        order_id=payload.order_id,
        supplier_id=payload.supplier_id,
        supplier_name=payload.supplier_name,
        status="placed",
        expected_delivery_date=payload.expected_delivery_date,
        expected_total_amount=payload.expected_total_amount,
        invoice_id=payload.invoice_id,
        notes=payload.notes,
        items=items,
    )
    projection.publish_purchase_order(payload.order_id, venue_id, {"status": "placed"})
    return {"status": "ok", "order_id": payload.order_id}


@router.post("/orders/received")
async def receive_order(
    payload: ReceiveOrderPayload,
    _admin: AdminDep,
    venue_id: VenueDep,
    session: DbDep,
    projection: InventoryProjectionDep,
) -> dict:
    if not payload.order_id:
        raise HTTPException(status_code=400, detail="order_id is required")
    if not payload.items:
        raise HTTPException(status_code=400, detail="at least one item must be received")
    received = [
        {"inventory_sku": it.inventory_sku, "quantity": it.quantity} for it in payload.items
    ]
    await crud.receive_purchase_order(
        session, venue_id=venue_id, order_id=payload.order_id, received_items=received
    )
    projection.publish_purchase_order(payload.order_id, venue_id, {"status": "received"})
    return {"status": "ok", "order_id": payload.order_id}


@router.post("/orders/parse-document")
async def parse_order_document(
    _admin: AdminDep,
    session: DbDep,
    llm: LLMDep,
    file: UploadFile = File(...),
    supplier_id: str = Form(""),
) -> dict:
    """Parse a screenshot/PDF of a supplier order into reviewable line items.

    Parse-only: the document is never stored. Delegates to the order-import service,
    which builds the catalog, consults learned aliases, runs the LLM extract+match
    step, then deterministically overrides lines from the alias memory.
    """
    mime = (file.content_type or "").lower().split(";")[0].strip()
    if mime not in ALLOWED_DOC_MIMES:
        raise HTTPException(415, f"Unsupported file type '{mime}'. Allowed: PDF, JPEG, PNG, WebP.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large — max ~5MB.")

    result = await service.parse_order_document(
        llm, session, data=data, mime=mime, supplier_id=supplier_id or None
    )
    if result is None:
        raise HTTPException(502, "Could not read the document — try a clearer photo.")
    return result


@router.post("/orders/import-aliases")
async def import_aliases(
    payload: ImportAliasesPayload,
    admin: AdminDep,
    venue_id: VenueDep,
    session: DbDep,
) -> dict:
    """Bulk-upsert confirmed document-line -> inventory-sku mappings for a supplier."""
    sid = (payload.supplier_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="supplier_id is required")

    mappings = []
    for m in payload.mappings:
        raw_name = (m.raw_name or "").strip()
        sku = (m.inventory_sku or "").strip()
        if not raw_name or not sku:
            continue
        mappings.append(
            {
                "raw_name": raw_name,
                "raw_name_norm": service.normalize_name(raw_name),
                "inventory_sku": sku,
                "created_by": admin.uid,
            }
        )
    upserted = await crud.upsert_order_import_aliases(
        session, venue_id=venue_id, supplier_id=sid, mappings=mappings
    )
    return {"upserted": upserted}


__all__ = ["router", "get_inventory_projection"]
