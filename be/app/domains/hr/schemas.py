"""Pydantic request models for the HR & access domain.

Create-style models carry sensible defaults so a builder FE can ``POST {}`` to make an
empty draft (the house convention) — the router then validates the few genuinely
required fields (e.g. an admin_user needs a login identity; an employee needs a name +
one of email/phone). Nothing here is cuisine/brand/locale-specific.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# --- admin users ------------------------------------------------------------


class AdminUserCreate(BaseModel):
    """A new admin_users row. ``uid`` and/or ``email`` is the login identity match key."""

    uid: str | None = None
    email: str | None = None
    role: str = "full_admin"
    allowed_pages: list[str] = Field(default_factory=list)
    telegram_user_id: int | None = None
    venue_ids: list[str] = Field(default_factory=list)


class AdminUserUpdate(BaseModel):
    uid: str | None = None
    email: str | None = None
    role: str | None = None
    allowed_pages: list[str] | None = None
    is_active: bool | None = None
    telegram_user_id: int | None = None
    venue_ids: list[str] | None = None


class ManagerEmployeesSet(BaseModel):
    employee_ids: list[int] = Field(default_factory=list)


# --- employees --------------------------------------------------------------


class EmployeeCreate(BaseModel):
    name: str = Field(default="")
    surname: str = Field(default="")
    email: str | None = None
    phone: str | None = None
    default_role_id: int | None = None
    is_stable_schedule: bool = False
    pay_type: str = "hourly"
    pay_rate: float | None = None
    # ISO "YYYY-MM-DD" — stored as TEXT (portable; no asyncpg DATE binding).
    birth_date: str | None = None
    group_ids: list[int] = Field(default_factory=list)


class GroupsSet(BaseModel):
    group_ids: list[int] = Field(default_factory=list)


# --- job roles --------------------------------------------------------------


class JobRoleCreate(BaseModel):
    name: str = Field(default="")
    start_time: str | None = None  # "HH:MM"
    end_time: str | None = None  # "HH:MM"
    color: str = "#60a5fa"


class JobRoleUpdate(BaseModel):
    name: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    color: str | None = None
    is_active: bool | None = None


# --- employee groups --------------------------------------------------------


class EmployeeGroupCreate(BaseModel):
    name: str = Field(default="")
    color: str = "#60a5fa"


class EmployeeGroupUpdate(BaseModel):
    name: str | None = None
    color: str | None = None
    is_active: bool | None = None


class GroupMembersSet(BaseModel):
    employee_ids: list[int] = Field(default_factory=list)


__all__ = [
    "AdminUserCreate",
    "AdminUserUpdate",
    "ManagerEmployeesSet",
    "EmployeeCreate",
    "GroupsSet",
    "JobRoleCreate",
    "JobRoleUpdate",
    "EmployeeGroupCreate",
    "EmployeeGroupUpdate",
    "GroupMembersSet",
]
