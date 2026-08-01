"""Pydantic request models for the scheduling / timesheet / time-off domain.

Create-style models carry sensible defaults (house convention: a builder FE can
``POST {}`` for a draft); the router validates the genuinely required fields. Nothing
here is cuisine/brand/locale-specific. Roles/groups/employee mutations are NOT here —
they belong to the hr domain (``/job-roles``, ``/employee-groups``, ``/employees``).
"""

from __future__ import annotations

from pydantic import BaseModel

# --- scheduled shifts -------------------------------------------------------


class ShiftAssign(BaseModel):
    employee_id: int = 0
    role_id: int = 0
    shift_date: str = ""  # "YYYY-MM-DD"
    start_time: str = ""  # "HH:MM"
    end_time: str = ""  # "HH:MM"
    notes: str | None = None
    status: str | None = None  # 'draft' (default) .. 'cancelled' (recurring skip)


class ShiftUpdate(BaseModel):
    role_id: int | None = None
    start_time: str | None = None
    end_time: str | None = None
    notes: str | None = None
    status: str | None = None


class BulkPublish(BaseModel):
    shift_ids: list[int] | None = None


class ApproveAll(BaseModel):
    week_start: str = ""  # "YYYY-MM-DD" (Sunday of the week to approve)


# --- change requests --------------------------------------------------------


class ChangeRequest(BaseModel):
    requested_start_time: str = ""  # "HH:MM"
    requested_end_time: str = ""  # "HH:MM"
    reason: str | None = None


class RecurringChangeRequest(BaseModel):
    recurring_shift_id: int = 0
    target_date: str = ""  # "YYYY-MM-DD"
    requested_start_time: str = ""  # "HH:MM"
    requested_end_time: str = ""  # "HH:MM"
    reason: str | None = None


class RejectRequest(BaseModel):
    # Optional reject reason; whole body optional so a no-body POST still works.
    note: str | None = None


# --- availability -----------------------------------------------------------


class AvailabilityItem(BaseModel):
    day_of_week: int = 0  # 0=Sun .. 6=Sat
    is_available: bool = True
    note: str | None = None


# --- recurring shifts -------------------------------------------------------


class RecurringShiftCreate(BaseModel):
    employee_id: int = 0
    day_of_week: int = 0  # 0..6
    role_id: int = 0
    start_time: str = ""  # "HH:MM"
    end_time: str = ""  # "HH:MM"
    start_date: str | None = None  # ISO "YYYY-MM-DD"; defaults to created_at date


class RecurringShiftUpdate(BaseModel):
    role_id: int | None = None
    start_time: str | None = None
    end_time: str | None = None
    is_active: bool | None = None


# --- timesheet --------------------------------------------------------------


class TimesheetConfirm(BaseModel):
    year: int = 0
    month: int = 0


class TimesheetCorrection(BaseModel):
    shift_id: int = 0
    requested_start_time: str | None = None
    requested_end_time: str | None = None
    correction_note: str | None = None


class ReportHours(BaseModel):
    shift_date: str = ""  # "YYYY-MM-DD"
    requested_start_time: str = ""  # "HH:MM"
    requested_end_time: str = ""  # "HH:MM"
    note: str | None = None


# --- time off ---------------------------------------------------------------


class TimeOffCreate(BaseModel):
    type: str = "vacation"  # 'vacation' | 'sick'
    date_from: str = ""  # "YYYY-MM-DD"
    date_to: str = ""  # "YYYY-MM-DD"
    note: str | None = None


class TimeOffRespond(BaseModel):
    status: str = ""  # 'approved' | 'rejected'
    note: str | None = None


class AdminTimeOffCreate(BaseModel):
    employee_id: int = 0
    type: str = "vacation"
    date_from: str = ""
    date_to: str = ""
    note: str | None = None


# --- working hours (config) -------------------------------------------------


class WorkingHoursSet(BaseModel):
    year: int = 0
    month: int = 0
    hours: float = 0.0


__all__ = [
    "ShiftAssign",
    "ShiftUpdate",
    "BulkPublish",
    "ApproveAll",
    "ChangeRequest",
    "RecurringChangeRequest",
    "RejectRequest",
    "AvailabilityItem",
    "RecurringShiftCreate",
    "RecurringShiftUpdate",
    "TimesheetConfirm",
    "TimesheetCorrection",
    "ReportHours",
    "TimeOffCreate",
    "TimeOffRespond",
    "AdminTimeOffCreate",
    "WorkingHoursSet",
]
