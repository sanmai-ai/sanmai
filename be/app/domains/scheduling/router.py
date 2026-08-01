"""Scheduling / timesheet / time-off router — thin, identity-seam gated endpoints.

Two access tiers:

* **admin** — DB-RBAC via :func:`be.app.deps.require_admin`. Schedule management
  (weeks, shifts, recurring, change-request approve/reject) requires the ``schedule``
  page; timesheet-correction + time-off approval and working-hours config require the
  ``timesheets`` page. ``full_admin`` bypasses.
* **staff-self** — :class:`PrincipalDep` verifies the token; the calling *employee* is
  resolved from the VERIFIED identity (email/phone claims) via
  ``hr.crud.find_employee_by_identity`` — NEVER a query-param/body email (this closes
  the live security hole). Every staff mutation is then constrained to that
  employee_id, so one member can't read or touch another's shifts/availability/
  timesheet/time-off.

Notifications (Telegram approval cards) are DEFERRED — wire later behind a Notifier
adapter; no ``background_tasks`` here.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from be.adapters.storage.base import Storage
from be.adapters.types import Principal
from be.app.deps import (
    AdminContext,
    DbDep,
    PrincipalDep,
    VenueDep,
    get_storage,
    require_admin,
)
from be.app.domains.hr import crud as hr_crud
from be.app.domains.hr import service as hr_service
from be.app.domains.scheduling import crud, service
from be.app.domains.scheduling.schemas import (
    AdminTimeOffCreate,
    ApproveAll,
    AvailabilityItem,
    BulkPublish,
    ChangeRequest,
    RecurringChangeRequest,
    RecurringShiftCreate,
    RecurringShiftUpdate,
    RejectRequest,
    ReportHours,
    ShiftAssign,
    ShiftUpdate,
    TimeOffCreate,
    TimeOffRespond,
    TimesheetConfirm,
    TimesheetCorrection,
    WorkingHoursSet,
)

router = APIRouter(tags=["scheduling"])

ScheduleAdmin = Annotated[AdminContext, Depends(require_admin("schedule"))]
TimesheetsAdmin = Annotated[AdminContext, Depends(require_admin("timesheets"))]
StorageDep = Annotated[Storage, Depends(get_storage)]

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB


# --- staff identity resolution ---------------------------------------------


async def _resolve_employee(principal: Principal, session: DbDep) -> dict:
    """Resolve the calling employee from the VERIFIED token identity, or 404.

    email/phone come from token claims only — never from a query param or body.
    """
    email = hr_service.normalize_email(principal.claims.get("email"))
    try:
        phone = hr_service.normalize_phone(principal.claims.get("phone"))
    except ValueError:
        phone = None
    emp = await hr_crud.find_employee_by_identity(session, email=email, phone=phone)
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return emp


async def _admin_id(admin: AdminContext, session: DbDep) -> int | None:
    return await crud.get_admin_id(session, uid=admin.uid, email=admin.email)


def _iso(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


# ══════════════════════════════════════════════════════════════════
# ADMIN — SCHEDULE WEEKS + SHIFTS  (require_admin("schedule"))
# ══════════════════════════════════════════════════════════════════


@router.get("/schedule/weeks")
async def list_weeks(
    _admin: ScheduleAdmin,
    venue_id: VenueDep,
    session: DbDep,
    from_date: date | None = Query(None, alias="from"),
    to_date: date | None = Query(None, alias="to"),
) -> list[dict]:
    today = date.today()
    start = from_date or service.sunday_of_week(today)
    end = to_date or date.fromordinal(start.toordinal() + 28)
    return await crud.list_weeks(
        session, venue_id=venue_id, from_date=start.isoformat(), to_date=end.isoformat()
    )


@router.post("/schedule/weeks")
async def create_week(
    _admin: ScheduleAdmin,
    venue_id: VenueDep,
    session: DbDep,
    week_start: str = Query(...),
) -> dict:
    try:
        ws = service.parse_date(week_start)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not service.is_sunday(ws):
        raise HTTPException(status_code=400, detail="week_start must be a Sunday")
    return await crud.create_week(session, venue_id=venue_id, week_start=ws.isoformat())


@router.post("/schedule/weeks/{schedule_id}/publish")
async def publish_week(
    schedule_id: int,
    _admin: ScheduleAdmin,
    session: DbDep,
    payload: BulkPublish | None = None,
) -> dict:
    shift_ids = payload.shift_ids if payload else None
    count = await crud.publish_week(session, schedule_id=schedule_id, shift_ids=shift_ids)
    return {"published": count}


@router.post("/schedule/weeks/{schedule_id}/unpublish")
async def unpublish_week(
    schedule_id: int, _admin: ScheduleAdmin, session: DbDep
) -> dict:
    count = await crud.unpublish_week(session, schedule_id=schedule_id)
    return {"unpublished": count}


@router.get("/schedule/weeks/{schedule_id}/shifts")
async def get_week_shifts(
    schedule_id: int, _admin: ScheduleAdmin, session: DbDep
) -> list[dict]:
    return await crud.get_week_shifts(session, schedule_id=schedule_id)


@router.post("/schedule/weeks/{schedule_id}/shifts")
async def assign_shift(
    schedule_id: int,
    payload: ShiftAssign,
    _admin: ScheduleAdmin,
    venue_id: VenueDep,
    session: DbDep,
) -> dict:
    status = payload.status or "draft"
    if status not in service.VALID_SHIFT_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status")
    if not payload.employee_id or not payload.role_id:
        raise HTTPException(status_code=400, detail="employee_id and role_id are required")
    try:
        shift_date = service.parse_date(payload.shift_date).isoformat()
        start_time = service.require_time(payload.start_time)
        end_time = service.require_time(payload.end_time)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await crud.assign_shift(
        session,
        venue_id=venue_id,
        schedule_id=schedule_id,
        employee_id=payload.employee_id,
        role_id=payload.role_id,
        shift_date=shift_date,
        start_time=start_time,
        end_time=end_time,
        notes=payload.notes,
        status=status,
    )


@router.put("/schedule/shifts/{shift_id}")
async def update_shift(
    shift_id: int, payload: ShiftUpdate, _admin: ScheduleAdmin, session: DbDep
) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        if "start_time" in fields:
            fields["start_time"] = service.require_time(fields["start_time"])
        if "end_time" in fields:
            fields["end_time"] = service.require_time(fields["end_time"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if fields.get("status") is not None and fields["status"] not in service.VALID_SHIFT_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status")
    updated = await crud.update_shift(session, shift_id=shift_id, fields=fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="shift not found")
    return {"ok": True}


@router.delete("/schedule/shifts/{shift_id}")
async def delete_shift(shift_id: int, _admin: ScheduleAdmin, session: DbDep) -> dict:
    if not await crud.delete_shift(session, shift_id=shift_id):
        raise HTTPException(status_code=404, detail="shift not found")
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# ADMIN — RECURRING SHIFTS  (require_admin("schedule"))
# ══════════════════════════════════════════════════════════════════


@router.get("/schedule/recurring-shifts")
async def list_recurring_shifts(
    _admin: ScheduleAdmin,
    session: DbDep,
    employee_id: int | None = Query(None),
) -> list[dict]:
    return await crud.list_recurring_shifts(session, employee_id=employee_id)


@router.post("/schedule/recurring-shifts")
async def create_recurring_shift(
    payload: RecurringShiftCreate,
    _admin: ScheduleAdmin,
    venue_id: VenueDep,
    session: DbDep,
) -> dict:
    if not payload.employee_id or not payload.role_id:
        raise HTTPException(status_code=400, detail="employee_id and role_id are required")
    if not 0 <= payload.day_of_week <= 6:
        raise HTTPException(status_code=400, detail="day_of_week must be 0..6")
    try:
        start_time = service.require_time(payload.start_time)
        end_time = service.require_time(payload.end_time)
        start_date = (
            service.parse_date(payload.start_date).isoformat()
            if payload.start_date
            else date.today().isoformat()
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await crud.create_recurring_shift(
        session,
        venue_id=venue_id,
        employee_id=payload.employee_id,
        day_of_week=payload.day_of_week,
        role_id=payload.role_id,
        start_time=start_time,
        end_time=end_time,
        start_date=start_date,
    )


@router.put("/schedule/recurring-shifts/{shift_id}")
async def update_recurring_shift(
    shift_id: int, payload: RecurringShiftUpdate, _admin: ScheduleAdmin, session: DbDep
) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        if "start_time" in fields:
            fields["start_time"] = service.require_time(fields["start_time"])
        if "end_time" in fields:
            fields["end_time"] = service.require_time(fields["end_time"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    updated = await crud.update_recurring_shift(session, shift_id=shift_id, fields=fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="recurring shift not found")
    return {"ok": True}


@router.delete("/schedule/recurring-shifts/{shift_id}")
async def delete_recurring_shift(
    shift_id: int, _admin: ScheduleAdmin, session: DbDep
) -> dict:
    if not await crud.delete_recurring_shift(session, shift_id=shift_id):
        raise HTTPException(status_code=404, detail="recurring shift not found")
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# ADMIN — SCHEDULE-CHANGE REQUESTS  (require_admin("schedule"))
# ══════════════════════════════════════════════════════════════════


@router.get("/schedule/change-requests")
async def list_change_requests(
    _admin: ScheduleAdmin,
    session: DbDep,
    status: str | None = Query(None),
) -> list[dict]:
    return await crud.list_change_requests(session, status=status)


@router.post("/schedule/change-requests/{request_id}/admin-approve")
async def admin_approve_change(
    request_id: int, admin: ScheduleAdmin, session: DbDep
) -> dict:
    aid = await _admin_id(admin, session)
    if not await crud.approve_schedule_change(
        session, request_id=request_id, admin_id=aid
    ):
        raise HTTPException(status_code=404, detail="request not found or not pending")
    return {"ok": True}


@router.post("/schedule/change-requests/{request_id}/admin-reject")
async def admin_reject_change(
    request_id: int,
    admin: ScheduleAdmin,
    session: DbDep,
    payload: RejectRequest | None = None,
) -> dict:
    aid = await _admin_id(admin, session)
    note = payload.note if payload else None
    if not await crud.reject_schedule_change(
        session, request_id=request_id, admin_id=aid, note=note
    ):
        raise HTTPException(status_code=404, detail="request not found or not pending")
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# STAFF-SELF — SCHEDULE VIEW + DECISIONS  (require_principal)
# ══════════════════════════════════════════════════════════════════


@router.get("/schedule/my-shifts")
async def my_shifts(
    principal: PrincipalDep,
    session: DbDep,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
) -> list[dict]:
    emp = await _resolve_employee(principal, session)
    is_stable = await crud.get_employee_stable(session, employee_id=emp["id"])
    return await crud.my_shifts(
        session,
        employee_id=emp["id"],
        is_stable=is_stable,
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
    )


@router.post("/schedule/weeks/approve-all")
async def approve_all_week(
    payload: ApproveAll, principal: PrincipalDep, session: DbDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    try:
        week_start = service.parse_date(payload.week_start)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    week_end = date.fromordinal(week_start.toordinal() + 6)
    approved = await crud.approve_all_week(
        session,
        employee_id=emp["id"],
        week_start=week_start.isoformat(),
        week_end=week_end.isoformat(),
    )
    return {"approved": approved}


@router.post("/schedule/shifts/{shift_id}/approve")
async def approve_shift(
    shift_id: int, principal: PrincipalDep, session: DbDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    ok = await crud.set_shift_decision(
        session, shift_id=shift_id, employee_id=emp["id"], decision="approved"
    )
    if not ok:
        raise HTTPException(status_code=400, detail="shift not found or not pending")
    return {"ok": True}


@router.post("/schedule/shifts/{shift_id}/decline")
async def decline_shift(
    shift_id: int, principal: PrincipalDep, session: DbDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    ok = await crud.set_shift_decision(
        session, shift_id=shift_id, employee_id=emp["id"], decision="declined"
    )
    if not ok:
        raise HTTPException(status_code=400, detail="shift not found or not pending")
    return {"ok": True}


@router.post("/schedule/shifts/{shift_id}/request-change")
async def request_change(
    shift_id: int,
    payload: ChangeRequest,
    principal: PrincipalDep,
    venue_id: VenueDep,
    session: DbDep,
) -> dict:
    emp = await _resolve_employee(principal, session)
    shift = await crud.get_scheduled_shift(session, shift_id=shift_id)
    if shift is None:
        raise HTTPException(status_code=404, detail="shift not found")
    if shift["employee_id"] != emp["id"]:
        raise HTTPException(status_code=403, detail="shift does not belong to you")
    try:
        start = service.require_time(payload.requested_start_time)
        end = service.require_time(payload.requested_end_time)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cid = await crud.create_schedule_change(
        session,
        venue_id=venue_id,
        employee_id=emp["id"],
        scheduled_shift_id=shift_id,
        original_start=shift["start_time"],
        original_end=shift["end_time"],
        requested_start=start,
        requested_end=end,
        reason=payload.reason,
    )
    return {"id": cid}


@router.post("/schedule/recurring-shifts/request-change")
async def request_recurring_change(
    payload: RecurringChangeRequest,
    principal: PrincipalDep,
    venue_id: VenueDep,
    session: DbDep,
) -> dict:
    emp = await _resolve_employee(principal, session)
    rs = await crud.get_recurring_shift(session, shift_id=payload.recurring_shift_id)
    if rs is None:
        raise HTTPException(status_code=404, detail="recurring shift not found")
    if rs["employee_id"] != emp["id"]:
        raise HTTPException(status_code=403, detail="recurring shift does not belong to you")
    try:
        target = service.parse_date(payload.target_date).isoformat()
        start = service.require_time(payload.requested_start_time)
        end = service.require_time(payload.requested_end_time)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cid = await crud.create_recurring_change(
        session,
        venue_id=venue_id,
        employee_id=emp["id"],
        recurring_shift_id=payload.recurring_shift_id,
        target_date=target,
        original_start=rs["start_time"],
        original_end=rs["end_time"],
        requested_start=start,
        requested_end=end,
        reason=payload.reason,
    )
    return {"id": cid}


# ══════════════════════════════════════════════════════════════════
# STAFF-SELF — AVAILABILITY  (require_principal)
# ══════════════════════════════════════════════════════════════════


@router.get("/schedule/availability")
async def get_availability(principal: PrincipalDep, session: DbDep) -> list[dict]:
    emp = await _resolve_employee(principal, session)
    return await crud.get_availability(session, employee_id=emp["id"])


@router.put("/schedule/availability")
async def set_availability(
    items: list[AvailabilityItem], principal: PrincipalDep, session: DbDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    for item in items:
        if not 0 <= item.day_of_week <= 6:
            raise HTTPException(status_code=400, detail="day_of_week must be 0..6")
    await crud.set_availability(
        session,
        employee_id=emp["id"],
        items=[i.model_dump() for i in items],
    )
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# STAFF-SELF — TIMESHEET  (require_principal)
# ══════════════════════════════════════════════════════════════════


@router.get("/staff/timesheet/{year}/{month}")
async def get_timesheet(
    year: int, month: int, principal: PrincipalDep, session: DbDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    first, last = service.month_bounds(year, month)
    shifts = await crud.get_monthly_shifts(
        session, employee_id=emp["id"], first=first, last=last
    )
    conf = await crud.get_confirmation(
        session, employee_id=emp["id"], year=year, month=month
    )
    corrections = await crud.get_monthly_corrections(
        session, employee_id=emp["id"], first=first, last=last
    )
    time_off = await crud.get_own_time_off(
        session, employee_id=emp["id"], from_date=first, to_date=last
    )
    month_closed = service.month_is_closed(year, month, date.today())

    total_minutes = 0
    shift_out: list[dict] = []
    finished_confirmable = 0
    unconfirmed_confirmable = 0
    has_open = False
    has_blocking_pending = False
    for s in shifts:
        minutes = service.shift_minutes(s["start_time"], s["end_time"])
        if minutes:
            total_minutes += minutes
        is_open = s["end_time"] is None
        pending = bool(s["pending_correction"])
        confirmed = bool(s["confirmed"])
        auto_unacked = s["source"] == "auto_closed" and not bool(s["auto_close_ack"])
        confirmable = not is_open and not pending and not auto_unacked
        if confirmable:
            finished_confirmable += 1
            if not confirmed:
                unconfirmed_confirmable += 1
        if is_open and not pending:
            has_open = True
        if pending:
            has_blocking_pending = True
        shift_out.append(
            {
                "id": s["id"],
                "date": _iso(s["shift_date"]),
                "start_time": s["start_time"],
                "end_time": s["end_time"],
                "duration": service.minutes_to_duration(minutes),
                "confirmed": confirmed,
                "is_open": is_open,
                "pending_correction": pending,
                "locked": confirmed and month_closed,
            }
        )

    confirmed_month = (
        finished_confirmable > 0
        and unconfirmed_confirmable == 0
        and not has_open
        and not has_blocking_pending
    )
    total_h, total_m = divmod(total_minutes, 60)
    return {
        "year": year,
        "month": month,
        "confirmed": confirmed_month,
        "confirmed_at": _iso(conf["confirmed_at"]) if conf else None,
        "finished_confirmable": finished_confirmable,
        "unconfirmed_confirmable": unconfirmed_confirmable,
        "has_open": has_open,
        "has_blocking_pending": has_blocking_pending,
        "shifts": shift_out,
        "corrections": [
            {
                "id": c["id"],
                "shift_id": c["shift_id"],
                "requested_start_time": c["requested_start_time"],
                "requested_end_time": c["requested_end_time"],
                "correction_note": c["correction_note"],
                "status": c["status"],
                "admin_response_note": c["admin_response_note"],
            }
            for c in corrections
        ],
        "time_off": [
            {
                "id": t["id"],
                "type": t["type"],
                "date_from": _iso(t["date_from"]),
                "date_to": _iso(t["date_to"]),
                "note": t["note"],
                "document_url": t["document_url"],
                "status": t["status"],
                "admin_response_note": t["admin_response_note"],
            }
            for t in time_off
        ],
        "total_hours": f"{total_h}h {total_m}m",
    }


@router.post("/staff/timesheet/shift/{shift_id}/confirm")
async def confirm_single_shift(
    shift_id: int, principal: PrincipalDep, session: DbDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    shift = await crud.get_owned_shift(session, shift_id=shift_id, employee_id=emp["id"])
    if shift is None:
        raise HTTPException(status_code=404, detail="shift not found")
    if shift["end_time"] is None:
        raise HTTPException(status_code=400, detail="can't confirm an unfinished shift")
    if shift["pending_correction"]:
        raise HTTPException(status_code=400, detail="waiting for correction approval")
    if shift["source"] == "auto_closed" and not bool(shift["auto_close_ack"]):
        raise HTTPException(status_code=400, detail="review the automatic clock-out first")
    await crud.confirm_shift(session, shift_id=shift_id, employee_id=emp["id"])
    return {"ok": True}


@router.post("/staff/timesheet/confirm")
async def confirm_timesheet(
    payload: TimesheetConfirm, principal: PrincipalDep, session: DbDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    first, last = service.month_bounds(payload.year, payload.month)
    confirmed = await crud.confirm_month_shifts(
        session, employee_id=emp["id"], first=first, last=last
    )
    result = await crud.confirm_timesheet(
        session, employee_id=emp["id"], year=payload.year, month=payload.month
    )
    return {"ok": True, "confirmed": confirmed, "confirmed_at": result["confirmed_at"]}


@router.post("/staff/timesheet/correction")
async def submit_correction(
    payload: TimesheetCorrection,
    principal: PrincipalDep,
    venue_id: VenueDep,
    session: DbDep,
) -> dict:
    emp = await _resolve_employee(principal, session)
    if not await crud.verify_shift_owner(
        session, shift_id=payload.shift_id, employee_id=emp["id"]
    ):
        raise HTTPException(status_code=403, detail="shift does not belong to you")
    lock = await crud.get_shift_lock_info(session, shift_id=payload.shift_id)
    if lock and lock["confirmed"]:
        sd = lock["shift_date"]
        sd_date = sd if isinstance(sd, date) else date.fromisoformat(str(sd))
        if service.month_is_closed(sd_date.year, sd_date.month, date.today()):
            raise HTTPException(status_code=403, detail="this month is closed — contact your manager")
    cid = await crud.create_correction(
        session,
        venue_id=venue_id,
        employee_id=emp["id"],
        shift_id=payload.shift_id,
        start_time=payload.requested_start_time,
        end_time=payload.requested_end_time,
        note=payload.correction_note,
    )
    return {"id": cid}


@router.post("/staff/timesheet/report-hours")
async def report_hours(
    payload: ReportHours,
    principal: PrincipalDep,
    venue_id: VenueDep,
    session: DbDep,
) -> dict:
    emp = await _resolve_employee(principal, session)
    try:
        shift_date = service.parse_date(payload.shift_date).isoformat()
        start = service.require_time(payload.requested_start_time)
        end = service.require_time(payload.requested_end_time)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    shift_id = await crud.create_worked_shift(
        session,
        venue_id=venue_id,
        employee_id=emp["id"],
        shift_date=shift_date,
        start_time=start,
        end_time=end,
    )
    note = payload.note or "Reported hours — no clock-in for this shift"
    await crud.create_correction(
        session,
        venue_id=venue_id,
        employee_id=emp["id"],
        shift_id=shift_id,
        start_time=start,
        end_time=end,
        note=note,
    )
    return {"status": "sent_for_review", "shift_id": shift_id}


# ══════════════════════════════════════════════════════════════════
# STAFF-SELF — TIME OFF  (require_principal)
# ══════════════════════════════════════════════════════════════════


@router.get("/staff/time-off")
async def get_own_time_off(
    principal: PrincipalDep,
    session: DbDep,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
) -> list[dict]:
    emp = await _resolve_employee(principal, session)
    rows = await crud.get_own_time_off(
        session,
        employee_id=emp["id"],
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
    )
    return [
        {
            "id": r["id"],
            "type": r["type"],
            "date_from": _iso(r["date_from"]),
            "date_to": _iso(r["date_to"]),
            "note": r["note"],
            "document_url": r["document_url"],
            "status": r["status"],
            "admin_response_note": r["admin_response_note"],
        }
        for r in rows
    ]


@router.get("/staff/time-off/team")
async def get_team_time_off(
    principal: PrincipalDep,
    session: DbDep,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
) -> list[dict]:
    emp = await _resolve_employee(principal, session)
    rows = await crud.get_team_time_off(
        session,
        employee_id=emp["id"],
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
    )
    return [
        {
            "employee_name": r["employee_name"],
            "kind": r["kind"],
            "label": r["label"],
            "status": r["status"],
            "date_from": _iso(r["date_from"]),
            "date_to": _iso(r["date_to"]),
            "requested_on": _iso(r["requested_on"]) if r["requested_on"] else None,
        }
        for r in rows
    ]


@router.post("/staff/time-off")
async def create_time_off(
    payload: TimeOffCreate,
    principal: PrincipalDep,
    venue_id: VenueDep,
    session: DbDep,
) -> dict:
    emp = await _resolve_employee(principal, session)
    if payload.type not in service.VALID_TIME_OFF_TYPES:
        raise HTTPException(status_code=400, detail="type must be vacation or sick")
    try:
        df = service.parse_date(payload.date_from)
        dt = service.parse_date(payload.date_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if dt < df:
        raise HTTPException(status_code=400, detail="date_to must be on/after date_from")
    tid = await crud.create_time_off(
        session,
        venue_id=venue_id,
        employee_id=emp["id"],
        type_=payload.type,
        date_from=df.isoformat(),
        date_to=dt.isoformat(),
        note=payload.note,
    )
    return {"id": tid}


@router.post("/staff/time-off/{time_off_id}/upload")
async def upload_time_off_document(
    time_off_id: int,
    principal: PrincipalDep,
    session: DbDep,
    storage: StorageDep,
    file: UploadFile = File(...),
) -> dict:
    emp = await _resolve_employee(principal, session)
    record = await crud.verify_time_off_owner(
        session, time_off_id=time_off_id, employee_id=emp["id"]
    )
    if record is None:
        raise HTTPException(status_code=404, detail="time off request not found")
    if record["type"] != "sick":
        raise HTTPException(status_code=400, detail="document upload only for sick days")
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="file too large (max 10MB)")
    key = f"sick_docs/{emp['id']}/{time_off_id}_{file.filename}"
    url = await storage.put(
        key, contents, file.content_type or "application/octet-stream"
    )
    await crud.update_time_off_doc(session, time_off_id=time_off_id, url=url)
    return {"ok": True, "document_url": url}


@router.delete("/staff/time-off/{time_off_id}")
async def cancel_time_off(
    time_off_id: int, principal: PrincipalDep, session: DbDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    if not await crud.delete_time_off(
        session, time_off_id=time_off_id, employee_id=emp["id"]
    ):
        raise HTTPException(
            status_code=404, detail="time off request not found or already processed"
        )
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# ADMIN — TIMESHEET CORRECTIONS + TIME OFF  (require_admin("timesheets"))
# ══════════════════════════════════════════════════════════════════


@router.get("/admin/timesheet-corrections")
async def admin_list_corrections(
    _admin: TimesheetsAdmin,
    session: DbDep,
    status: str | None = Query(None),
) -> list[dict]:
    return await crud.list_corrections(session, status=status)


@router.post("/admin/timesheet-corrections/{correction_id}/approve")
async def admin_approve_correction(
    correction_id: int, admin: TimesheetsAdmin, session: DbDep
) -> dict:
    aid = await _admin_id(admin, session)
    if not await crud.approve_correction(
        session, correction_id=correction_id, admin_id=aid
    ):
        raise HTTPException(status_code=404, detail="correction not found or not pending")
    return {"ok": True}


@router.post("/admin/timesheet-corrections/{correction_id}/reject")
async def admin_reject_correction(
    correction_id: int,
    admin: TimesheetsAdmin,
    session: DbDep,
    payload: RejectRequest | None = None,
) -> dict:
    aid = await _admin_id(admin, session)
    note = payload.note if payload else None
    if not await crud.reject_correction(
        session, correction_id=correction_id, admin_id=aid, note=note
    ):
        raise HTTPException(status_code=404, detail="correction not found or not pending")
    return {"ok": True}


@router.get("/admin/time-off")
async def admin_list_time_off(
    _admin: TimesheetsAdmin,
    session: DbDep,
    status: str | None = Query(None),
    type: str | None = Query(None),  # noqa: A002 - matches the FE query key
    from_date: date | None = Query(None, alias="from"),
    to_date: date | None = Query(None, alias="to"),
) -> list[dict]:
    rows = await crud.list_time_off(
        session,
        status=status,
        type_=type,
        from_date=from_date.isoformat() if from_date else None,
        to_date=to_date.isoformat() if to_date else None,
    )
    return [
        {**r, "date_from": _iso(r["date_from"]), "date_to": _iso(r["date_to"])}
        for r in rows
    ]


@router.post("/admin/time-off/{time_off_id}/respond")
async def admin_respond_time_off(
    time_off_id: int,
    payload: TimeOffRespond,
    admin: TimesheetsAdmin,
    session: DbDep,
) -> dict:
    if payload.status not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="status must be approved or rejected")
    aid = await _admin_id(admin, session)
    if not await crud.respond_time_off(
        session,
        time_off_id=time_off_id,
        status=payload.status,
        admin_id=aid,
        note=payload.note,
    ):
        raise HTTPException(status_code=404, detail="request not found or already processed")
    return {"ok": True}


@router.post("/admin/time-off/admin-create")
async def admin_create_time_off(
    payload: AdminTimeOffCreate,
    admin: TimesheetsAdmin,
    venue_id: VenueDep,
    session: DbDep,
) -> dict:
    if not payload.employee_id:
        raise HTTPException(status_code=400, detail="employee_id is required")
    if payload.type not in service.VALID_TIME_OFF_TYPES:
        raise HTTPException(status_code=400, detail="type must be vacation or sick")
    try:
        df = service.parse_date(payload.date_from)
        dt = service.parse_date(payload.date_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if dt < df:
        raise HTTPException(status_code=400, detail="date_to must be on/after date_from")
    aid = await _admin_id(admin, session)
    tid = await crud.admin_create_time_off(
        session,
        venue_id=venue_id,
        employee_id=payload.employee_id,
        type_=payload.type,
        date_from=df.isoformat(),
        date_to=dt.isoformat(),
        note=payload.note,
        admin_id=aid,
    )
    return {"id": tid}


@router.delete("/admin/time-off/{time_off_id}")
async def admin_delete_time_off(
    time_off_id: int, _admin: TimesheetsAdmin, session: DbDep
) -> dict:
    if not await crud.admin_delete_time_off(session, time_off_id=time_off_id):
        raise HTTPException(status_code=404, detail="time-off request not found")
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# ADMIN — MONTHLY WORKING HOURS  (require_admin("timesheets"))
# ══════════════════════════════════════════════════════════════════


@router.get("/admin/working-hours")
async def admin_get_working_hours(
    _admin: TimesheetsAdmin,
    venue_id: VenueDep,
    session: DbDep,
    year: int | None = Query(None),
    month: int | None = Query(None),
) -> dict | list[dict]:
    if year is not None and month is not None:
        row = await crud.get_working_hours(
            session, venue_id=venue_id, year=year, month=month
        )
        return row or {"year": year, "month": month, "hours": None}
    return await crud.list_working_hours(session, venue_id=venue_id)


@router.put("/admin/working-hours")
async def admin_set_working_hours(
    payload: WorkingHoursSet, _admin: TimesheetsAdmin, venue_id: VenueDep, session: DbDep
) -> dict:
    if not 1 <= payload.month <= 12:
        raise HTTPException(status_code=400, detail="month must be 1..12")
    if payload.hours < 0:
        raise HTTPException(status_code=400, detail="hours can't be negative")
    return await crud.upsert_working_hours(
        session,
        venue_id=venue_id,
        year=payload.year,
        month=payload.month,
        hours=payload.hours,
    )


__all__ = ["router"]
