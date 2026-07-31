"""Pydantic request/response models for the inventory domain.

Create-style models carry sensible defaults so a builder FE can ``POST {}`` to make
an empty draft (matches the house convention); the router validates the few fields
that are genuinely required. Nothing here is cuisine/brand/locale-specific.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# --- inventory items --------------------------------------------------------


class InventoryItemCreateRequest(BaseModel):
    """A new inventory item definition. ``sku`` is optional — slugged from ``name``."""

    sku: str = Field(default="")
    name: str = Field(default="")
    category: str = Field(default="")
    unit: str = Field(default="units")
    allowed_locations: list[str] = Field(default_factory=list)
    reorder_point: float | None = None
    max_threshold: float | None = None
    comment: str | None = None


class InventoryItemUpdateRequest(BaseModel):
    name: str | None = None
    category: str | None = None
    unit: str | None = None
    allowed_locations: list[str] | None = None
    reorder_point: float | None = None
    max_threshold: float | None = None
    comment: str | None = None
    is_active: bool | None = None


# --- stock ------------------------------------------------------------------


class StockUpdateRequest(BaseModel):
    sku: str = Field(default="")
    location: str = Field(default="")
    quantity: float = 0.0


class StockMoveRequest(BaseModel):
    sku: str = Field(default="")
    from_location: str = Field(default="")
    to_location: str = Field(default="")
    quantity: float = 0.0


# --- suppliers --------------------------------------------------------------


class SupplierUpsertRequest(BaseModel):
    supplier_id: str | None = None
    name: str = Field(default="")
    contact_person: str | None = None
    payment_type: str | None = None
    site: str | None = None
    chat_link: str | None = None
    comment: str | None = None
    categories: list[str] | None = None
    approx_delivery_time_info: str | None = None
    is_active: bool = True
    min_order_amount: float | None = None
    # Opt-in monthly billing day (1–31). NULL = per-order.
    payment_day: int | None = None
    # Delivery rules — order_cutoff_time is an "HH:MM" string (or null).
    order_cutoff_time: str | None = None
    delivery_lead_days: int | None = None
    delivery_working_days_only: bool = True


# --- supplier items ---------------------------------------------------------


class SupplierItemCreateRequest(BaseModel):
    supplier_id: str = Field(default="")
    supplier_sku: str | None = None
    pack_name: str | None = None
    pack_size: float | None = None
    price_per_pack: float | None = None
    tax_inclusive: bool = False
    is_primary_supplier: bool = False
    is_on_stop: bool = False
    link: str | None = None
    comment: str | None = None


class SupplierItemUpdateRequest(BaseModel):
    supplier_sku: str | None = None
    pack_name: str | None = None
    pack_size: float | None = None
    price_per_pack: float | None = None
    tax_inclusive: bool | None = None
    is_primary_supplier: bool | None = None
    is_on_stop: bool | None = None
    link: str | None = None
    comment: str | None = None


# --- purchasing -------------------------------------------------------------


class PurchaseItemPayload(BaseModel):
    inventory_sku: str = Field(default="")
    packs: int = Field(default=0, ge=0)
    pack_size: float | None = None
    pack_price: float | None = None
    supplier_id: str | None = None
    supplier_sku: str | None = None


class PlaceOrderPayload(BaseModel):
    order_id: str = Field(default="")
    supplier_id: str = Field(default="")
    supplier_name: str = Field(default="")
    expected_delivery_date: str | None = None
    expected_total_amount: float | None = None
    invoice_id: str | None = None
    notes: str | None = None
    items: list[PurchaseItemPayload] = Field(default_factory=list)


class ReceivedItemPayload(BaseModel):
    inventory_sku: str = Field(default="")
    quantity: float = Field(default=0.0, ge=0)


class ReceiveOrderPayload(BaseModel):
    order_id: str = Field(default="")
    items: list[ReceivedItemPayload] = Field(default_factory=list)


# --- order-import learned aliases -------------------------------------------


class AliasMappingPayload(BaseModel):
    raw_name: str = Field(default="")
    inventory_sku: str = Field(default="")


class ImportAliasesPayload(BaseModel):
    supplier_id: str = Field(default="")
    mappings: list[AliasMappingPayload] = Field(default_factory=list)


# --- supplier monthly payments ----------------------------------------------


class SupplierMonthlyPaymentUpsert(BaseModel):
    supplier_id: str = Field(default="")
    period_year: int = 0
    period_month: int = 0
    status: str = "pending"
    paid_amount: float | None = None
    paid_method: str | None = None
    proof_url: str | None = None
    notes: str | None = None


__all__ = [
    "InventoryItemCreateRequest",
    "InventoryItemUpdateRequest",
    "StockUpdateRequest",
    "StockMoveRequest",
    "SupplierUpsertRequest",
    "SupplierItemCreateRequest",
    "SupplierItemUpdateRequest",
    "PurchaseItemPayload",
    "PlaceOrderPayload",
    "ReceivedItemPayload",
    "ReceiveOrderPayload",
    "AliasMappingPayload",
    "ImportAliasesPayload",
    "SupplierMonthlyPaymentUpsert",
]
