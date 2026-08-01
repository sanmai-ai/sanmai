"""Pydantic request models for the payroll / compensation domain.

Create-style models carry sensible defaults so a builder FE can ``POST {}`` to make an
empty draft (the house convention); the router then validates the few genuinely required
fields. Nothing here is jurisdiction-specific — overtime math lives behind the
``PayrollProfile`` seam, and money is plain ``float`` bound through a numeric CAST in crud.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# --- pay rate + rules -------------------------------------------------------


class PayRateSet(BaseModel):
    """Set an employee's pay_type / pay_rate (writes the employees row)."""

    pay_type: str = "hourly"
    pay_rate: float | None = None


class PaymentRuleCreate(BaseModel):
    """A per-employee/day overtime bonus rule. ``employee_id``/``day_of_week`` NULL = all."""

    employee_id: int | None = None
    day_of_week: int | None = None
    after_minutes: int = 0
    bonus_percent: int = 0
    description: str | None = None
    active: bool = True


# --- one-time bonuses -------------------------------------------------------


class BonusCreate(BaseModel):
    employee_id: int | None = None
    year: int | None = None
    month: int | None = None
    amount: float | None = None
    comment: str | None = None


# --- payment calendar -------------------------------------------------------


class ScheduledPaymentCreate(BaseModel):
    name: str = ""
    category: str = "other"
    payment_type: str = "planned"
    amount: float = 0
    currency: str = "ILS"
    is_recurring: bool = False
    recurrence: dict[str, Any] | None = None
    start_date: str | None = None  # ISO "YYYY-MM-DD"
    end_date: str | None = None
    is_approximate: bool = False
    link_to_timesheets: bool = False
    notes: str | None = None


class ScheduledPaymentUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    payment_type: str | None = None
    amount: float | None = None
    currency: str | None = None
    is_recurring: bool | None = None
    recurrence: dict[str, Any] | None = None
    start_date: str | None = None
    end_date: str | None = None
    status: str | None = None
    is_approximate: bool | None = None
    link_to_timesheets: bool | None = None
    notes: str | None = None


class OccurrenceOverride(BaseModel):
    scheduled_payment_id: int | None = None
    due_date: str | None = None
    status: str = "pending"
    amount_override: float | None = None
    paid_at: str | None = None
    paid_amount: float | None = None
    paid_method: str | None = None
    notes: str | None = None


class InstallmentItem(BaseModel):
    due_date: str
    amount: float


class InstallmentPlanCreate(BaseModel):
    scheduled_payment_id: int | None = None
    source_due_date: str | None = None
    installments: list[InstallmentItem] = Field(default_factory=list)


class InstallmentUpdate(BaseModel):
    status: str = "paid"
    paid_amount: float | None = None
    paid_method: str | None = None
    notes: str | None = None


class BalanceUpdate(BaseModel):
    status: str = "paid"
    paid_amount: float | None = None
    paid_method: str | None = None
    notes: str | None = None


__all__ = [
    "PayRateSet",
    "PaymentRuleCreate",
    "BonusCreate",
    "ScheduledPaymentCreate",
    "ScheduledPaymentUpdate",
    "OccurrenceOverride",
    "InstallmentItem",
    "InstallmentPlanCreate",
    "InstallmentUpdate",
    "BalanceUpdate",
]
