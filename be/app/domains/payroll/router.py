"""Payroll / compensation router — thin endpoints, DB-RBAC gated via the identity seam.

Two access tiers:

* **admin** — every payroll WRITE (and the admin reads) requires the ``payroll`` page
  via :func:`be.app.deps.require_admin`. This is the security fix: in the legacy code
  pay-rate / bonus / payment-rule / calendar writes were UNAUTHENTICATED. ``full_admin``
  bypasses the page gate.
* **staff-self** — :class:`PrincipalDep` verifies the token; the calling employee is
  resolved from the VERIFIED identity (email/phone claims) via
  ``hr.crud.find_employee_by_identity`` — NEVER a query-param employee_id. Staff may read
  only their OWN pay summary + bonus log.

The pay computation is locale-neutral: overtime tiers/multipliers come from the injected
:class:`PayrollProfile` (:data:`be.app.deps.PayrollDep`), never hardcoded here.

DEFERRED (Notifier/Storage seams, not built here): Telegram approval on scheduled
payments; accountant export/email of payroll; the nightly auto-close scheduler.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from be.adapters.types import Principal
from be.app.deps import (
    AdminContext,
    DbDep,
    PayrollDep,
    PrincipalDep,
    VenueDep,
    require_admin,
)
from be.app.domains.hr import crud as hr_crud
from be.app.domains.hr import service as hr_service
from be.app.domains.payroll import crud, service
from be.app.domains.payroll.schemas import (
    BalanceUpdate,
    BonusCreate,
    InstallmentPlanCreate,
    InstallmentUpdate,
    OccurrenceOverride,
    PaymentRuleCreate,
    PayRateSet,
    ScheduledPaymentCreate,
    ScheduledPaymentUpdate,
)
from be.app.domains.scheduling import crud as sched_crud
from be.app.domains.scheduling import service as sched_service

router = APIRouter(tags=["payroll"])

# THE FIX: every payroll write is page-gated (full_admin bypasses).
PayrollAdmin = Annotated[AdminContext, Depends(require_admin("payroll"))]


# --- staff identity resolution ---------------------------------------------


async def _resolve_employee(principal: Principal, session: DbDep) -> dict:
    """Resolve the calling employee from the VERIFIED token identity, or 404."""
    email = hr_service.normalize_email(principal.claims.get("email"))
    try:
        phone = hr_service.normalize_phone(principal.claims.get("phone"))
    except ValueError:
        phone = None
    emp = await hr_crud.find_employee_by_identity(session, email=email, phone=phone)
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return emp


# ══════════════════════════════════════════════════════════════════
# ADMIN — PAY RATE  (require_admin("payroll"))
# ══════════════════════════════════════════════════════════════════


@router.put("/admin/employees/{employee_id}/pay-rate")
async def set_pay_rate(
    employee_id: int, payload: PayRateSet, _admin: PayrollAdmin, session: DbDep
) -> dict:
    try:
        service.validate_pay(payload.pay_type, payload.pay_rate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    updated = await crud.set_employee_pay(
        session,
        employee_id=employee_id,
        pay_type=payload.pay_type,
        pay_rate=payload.pay_rate,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return updated


# ══════════════════════════════════════════════════════════════════
# ADMIN — PAYMENT RULES  (require_admin("payroll"))
# ══════════════════════════════════════════════════════════════════


@router.get("/admin/payment-rules")
async def list_payment_rules(_admin: PayrollAdmin, session: DbDep) -> list[dict]:
    return await crud.list_payment_rules(session)


@router.post("/admin/payment-rules", status_code=201)
async def create_payment_rule(
    payload: PaymentRuleCreate, _admin: PayrollAdmin, venue_id: VenueDep, session: DbDep
) -> dict:
    if payload.day_of_week is not None and not 0 <= payload.day_of_week <= 6:
        raise HTTPException(status_code=400, detail="day_of_week must be 0..6")
    if payload.after_minutes < 0:
        raise HTTPException(status_code=400, detail="after_minutes can't be negative")
    if payload.bonus_percent < 0:
        raise HTTPException(status_code=400, detail="bonus_percent can't be negative")
    return await crud.create_payment_rule(
        session,
        venue_id=venue_id,
        employee_id=payload.employee_id,
        day_of_week=payload.day_of_week,
        after_minutes=payload.after_minutes,
        bonus_percent=payload.bonus_percent,
        description=payload.description,
        active=payload.active,
    )


@router.put("/admin/payment-rules/{rule_id}")
async def update_payment_rule(
    rule_id: int, payload: PaymentRuleCreate, _admin: PayrollAdmin, session: DbDep
) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    dow = fields.get("day_of_week")
    if dow is not None and not 0 <= dow <= 6:
        raise HTTPException(status_code=400, detail="day_of_week must be 0..6")
    updated = await crud.update_payment_rule(session, rule_id=rule_id, fields=fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="payment rule not found")
    return updated


@router.delete("/admin/payment-rules/{rule_id}")
async def delete_payment_rule(
    rule_id: int, _admin: PayrollAdmin, session: DbDep
) -> dict:
    if not await crud.delete_payment_rule(session, rule_id=rule_id):
        raise HTTPException(status_code=404, detail="payment rule not found")
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# ADMIN — ONE-TIME BONUSES  (require_admin("payroll"))
# ══════════════════════════════════════════════════════════════════


@router.get("/admin/bonuses")
async def list_bonuses(
    _admin: PayrollAdmin,
    session: DbDep,
    year: int = Query(...),
    month: int = Query(...),
) -> list[dict]:
    return await crud.list_monthly_bonuses(session, year=year, month=month)


@router.post("/admin/bonuses", status_code=201)
async def create_bonus(
    payload: BonusCreate, _admin: PayrollAdmin, venue_id: VenueDep, session: DbDep
) -> dict:
    if not payload.employee_id:
        raise HTTPException(status_code=400, detail="employee_id is required")
    try:
        year, month = service.validate_month(payload.year, payload.month)
        amount = service.validate_bonus_amount(payload.amount)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if await crud.get_employee_pay(session, employee_id=payload.employee_id) is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return await crud.create_bonus(
        session,
        venue_id=venue_id,
        employee_id=payload.employee_id,
        year=year,
        month=month,
        amount=amount,
        comment=payload.comment,
    )


@router.delete("/admin/bonuses/{bonus_id}")
async def delete_bonus(bonus_id: int, _admin: PayrollAdmin, session: DbDep) -> dict:
    if not await crud.delete_bonus(session, bonus_id=bonus_id):
        raise HTTPException(status_code=404, detail="bonus not found")
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# ADMIN — PAYROLL COMPUTE  (require_admin("payroll"))
# ══════════════════════════════════════════════════════════════════


@router.get("/admin/payroll/{employee_id}/{year}/{month}")
async def compute_employee_pay(
    employee_id: int,
    year: int,
    month: int,
    _admin: PayrollAdmin,
    venue_id: VenueDep,
    session: DbDep,
    profile: PayrollDep,
) -> dict:
    pay = await crud.get_employee_pay(session, employee_id=employee_id)
    if pay is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return await _compute_pay(
        session,
        profile,
        venue_id=venue_id,
        employee_id=employee_id,
        pay=pay,
        year=year,
        month=month,
    )


# ══════════════════════════════════════════════════════════════════
# ADMIN — PAYMENT CALENDAR  (require_admin("payroll"))
# ══════════════════════════════════════════════════════════════════


@router.get("/admin/scheduled-payments")
async def list_scheduled_payments(
    _admin: PayrollAdmin,
    session: DbDep,
    include_archived: bool = Query(False),
) -> list[dict]:
    return await crud.list_scheduled_payments(
        session, include_archived=include_archived
    )


@router.post("/admin/scheduled-payments", status_code=201)
async def create_scheduled_payment(
    payload: ScheduledPaymentCreate,
    admin: PayrollAdmin,
    venue_id: VenueDep,
    session: DbDep,
) -> dict:
    if not (payload.name or "").strip():
        raise HTTPException(status_code=400, detail="name is required")
    if not payload.start_date:
        raise HTTPException(status_code=400, detail="start_date is required")
    if payload.category not in service.VALID_PAYMENT_CATEGORIES:
        raise HTTPException(status_code=400, detail="invalid category")
    if payload.payment_type not in service.VALID_PAYMENT_TYPES:
        raise HTTPException(status_code=400, detail="invalid payment_type")
    try:
        sched_service.parse_date(payload.start_date)
        if payload.end_date:
            sched_service.parse_date(payload.end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await crud.create_scheduled_payment(
        session,
        venue_id=venue_id,
        created_by=admin.email or admin.uid,
        payload=payload.model_dump(),
    )


@router.put("/admin/scheduled-payments/{payment_id}")
async def update_scheduled_payment(
    payment_id: int,
    payload: ScheduledPaymentUpdate,
    _admin: PayrollAdmin,
    session: DbDep,
) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    updated = await crud.update_scheduled_payment(
        session, payment_id=payment_id, fields=fields
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="scheduled payment not found")
    return updated


@router.delete("/admin/scheduled-payments/{payment_id}")
async def delete_scheduled_payment(
    payment_id: int, _admin: PayrollAdmin, session: DbDep
) -> dict:
    if not await crud.delete_scheduled_payment(session, payment_id=payment_id):
        raise HTTPException(status_code=404, detail="scheduled payment not found")
    return {"ok": True}


@router.get("/admin/payment-calendar")
async def get_payment_calendar(
    _admin: PayrollAdmin,
    session: DbDep,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
) -> list[dict]:
    try:
        sched_service.parse_date(from_date)
        sched_service.parse_date(to_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await crud.build_calendar(
        session, window_start=from_date, window_end=to_date
    )


@router.post("/admin/payment-occurrences/override")
async def override_occurrence(
    payload: OccurrenceOverride, admin: PayrollAdmin, venue_id: VenueDep, session: DbDep
) -> dict:
    if not payload.scheduled_payment_id or not payload.due_date:
        raise HTTPException(
            status_code=400, detail="scheduled_payment_id and due_date are required"
        )
    if payload.status not in ("pending", "paid", "skipped"):
        raise HTTPException(status_code=400, detail="invalid status")
    if await crud.get_scheduled_payment(
        session, payment_id=payload.scheduled_payment_id
    ) is None:
        raise HTTPException(status_code=404, detail="scheduled payment not found")
    await crud.upsert_override(
        session,
        venue_id=venue_id,
        updated_by=admin.email or admin.uid,
        payload=payload.model_dump(),
    )
    return {"ok": True}


# ── installments ──


@router.get("/admin/scheduled-payments/{payment_id}/installments")
async def list_installments(
    payment_id: int,
    _admin: PayrollAdmin,
    session: DbDep,
    source_due_date: str = Query(...),
) -> list[dict]:
    return await crud.list_installments(
        session, scheduled_payment_id=payment_id, source_due_date=source_due_date
    )


@router.post("/admin/scheduled-payments/{payment_id}/installments", status_code=201)
async def create_installment_plan(
    payment_id: int,
    payload: InstallmentPlanCreate,
    admin: PayrollAdmin,
    venue_id: VenueDep,
    session: DbDep,
) -> list[dict]:
    if not payload.source_due_date:
        raise HTTPException(status_code=400, detail="source_due_date is required")
    if not 2 <= len(payload.installments) <= 4:
        raise HTTPException(status_code=400, detail="split into 2..4 installments")
    if await crud.get_scheduled_payment(session, payment_id=payment_id) is None:
        raise HTTPException(status_code=404, detail="scheduled payment not found")
    return await crud.create_installment_plan(
        session,
        venue_id=venue_id,
        created_by=admin.email or admin.uid,
        scheduled_payment_id=payment_id,
        source_due_date=payload.source_due_date,
        installments=[i.model_dump() for i in payload.installments],
    )


@router.put("/admin/installments/{installment_id}")
async def update_installment(
    installment_id: int,
    payload: InstallmentUpdate,
    _admin: PayrollAdmin,
    session: DbDep,
) -> dict:
    if payload.status not in ("pending", "paid"):
        raise HTTPException(status_code=400, detail="status must be pending or paid")
    if not await crud.update_installment(
        session, installment_id=installment_id, fields=payload.model_dump()
    ):
        raise HTTPException(status_code=404, detail="installment not found")
    return {"ok": True}


# ── balances ──


@router.get("/admin/payment-balances")
async def list_balances(
    _admin: PayrollAdmin,
    session: DbDep,
    only_pending: bool = Query(False),
) -> list[dict]:
    return await crud.list_balances(session, only_pending=only_pending)


@router.put("/admin/payment-balances/{balance_id}")
async def update_balance(
    balance_id: int, payload: BalanceUpdate, _admin: PayrollAdmin, session: DbDep
) -> dict:
    if payload.status not in ("pending", "paid"):
        raise HTTPException(status_code=400, detail="status must be pending or paid")
    if not await crud.update_balance(
        session, balance_id=balance_id, fields=payload.model_dump()
    ):
        raise HTTPException(status_code=404, detail="balance not found")
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# ADMIN — BONUS TASK LOG  (require_admin("payroll"))
# ══════════════════════════════════════════════════════════════════


@router.get("/admin/bonus-task-log")
async def list_bonus_task_log(
    _admin: PayrollAdmin,
    session: DbDep,
    only_unpaid: bool = Query(False),
) -> list[dict]:
    return await crud.list_bonus_task_log(session, only_unpaid=only_unpaid)


@router.post("/admin/bonus-task-log/{log_id}/mark-paid")
async def mark_bonus_paid(log_id: int, _admin: PayrollAdmin, session: DbDep) -> dict:
    if not await crud.mark_bonus_paid(session, log_id=log_id):
        raise HTTPException(status_code=404, detail="bonus log entry not found")
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# STAFF-SELF — MY PAY + BONUSES  (require_principal, own rows only)
# ══════════════════════════════════════════════════════════════════


@router.get("/staff/pay/{year}/{month}")
async def my_pay_summary(
    year: int,
    month: int,
    principal: PrincipalDep,
    venue_id: VenueDep,
    session: DbDep,
    profile: PayrollDep,
) -> dict:
    emp = await _resolve_employee(principal, session)
    pay = await crud.get_employee_pay(session, employee_id=emp["id"])
    if pay is None:  # pragma: no cover - resolved employee always has a pay row
        raise HTTPException(status_code=404, detail="employee not found")
    return await _compute_pay(
        session,
        profile,
        venue_id=venue_id,
        employee_id=emp["id"],
        pay=pay,
        year=year,
        month=month,
    )


@router.get("/staff/bonus-log")
async def my_bonus_log(principal: PrincipalDep, session: DbDep) -> list[dict]:
    emp = await _resolve_employee(principal, session)
    return await crud.list_employee_bonus_task_log(session, employee_id=emp["id"])


# --- shared compute helper --------------------------------------------------


async def _compute_pay(
    session: DbDep,
    profile: PayrollDep,
    *,
    venue_id: str,
    employee_id: int,
    pay: dict,
    year: int,
    month: int,
) -> dict:
    try:
        year, month = service.validate_month(year, month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    first, last = sched_service.month_bounds(year, month)
    shifts = await sched_crud.get_monthly_shifts(
        session, employee_id=employee_id, first=first, last=last
    )
    for s in shifts:
        s["employee_id"] = employee_id
    rules = await crud.get_active_payment_rules(session)
    wh = await sched_crud.get_working_hours(
        session, venue_id=venue_id, year=year, month=month
    )
    monthly_hours = wh.get("hours") if wh else None
    bonuses = await crud.get_employee_bonuses(
        session, employee_id=employee_id, year=year, month=month
    )
    result = service.compute_month_pay(
        shifts=shifts,
        rules=rules,
        profile=profile,
        pay_type=pay["pay_type"],
        pay_rate=pay["pay_rate"],
        monthly_hours=monthly_hours,
        one_time_bonuses=bonuses,
    )
    return {
        "employee_id": employee_id,
        "year": year,
        "month": month,
        "pay_type": pay["pay_type"],
        "pay_rate": pay["pay_rate"],
        "one_time_bonuses": bonuses,
        **result,
    }


__all__ = ["router"]
