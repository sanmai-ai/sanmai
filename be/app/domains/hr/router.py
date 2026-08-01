"""HR & access admin router — thin endpoints, DB-RBAC gated via the identity seam.

Authorization is DB-driven: :func:`be.app.deps.require_admin` verifies the Bearer token
via the :class:`IdentityProvider` seam, resolves the :class:`Principal` against
``admin_users``, and page/venue-gates. Admin-management routes require the ``users`` page;
employee CRUD the ``hr`` page; roles the ``roles`` page; groups the ``groups`` page
(``full_admin`` bypasses every page). Self-identity routes (``/staff/me``, ``/admin/access``)
resolve the caller from the VERIFIED token only — never a query-param email.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from be.app.deps import AdminContext, AdminDep, DbDep, PrincipalDep, VenueDep, require_admin
from be.app.domains.hr import crud, service
from be.app.domains.hr.schemas import (
    AdminUserCreate,
    AdminUserUpdate,
    EmployeeCreate,
    EmployeeGroupCreate,
    EmployeeGroupUpdate,
    GroupMembersSet,
    GroupsSet,
    JobRoleCreate,
    JobRoleUpdate,
    ManagerEmployeesSet,
)

router = APIRouter(tags=["hr"])

# Page-gated admin dependencies (full_admin bypasses each).
UsersAdmin = Annotated[AdminContext, Depends(require_admin("users"))]
HrAdmin = Annotated[AdminContext, Depends(require_admin("hr"))]
RolesAdmin = Annotated[AdminContext, Depends(require_admin("roles"))]
GroupsAdmin = Annotated[AdminContext, Depends(require_admin("groups"))]


# --- caller identity --------------------------------------------------------


@router.get("/admin/access")
async def admin_access(admin: AdminDep, session: DbDep) -> dict:
    """Resolve the caller's admin role/pages/managed-employees/venues from the token.

    Any active admin may read their own access; a non-admin token is rejected (403) by the
    gate before this body runs. Identity comes from the verified token, never a query param.
    """
    resolved = await crud.resolve_admin_access(
        session, uid=admin.uid, email=admin.email
    )
    if resolved is None:  # pragma: no cover - gate guarantees an admin row exists
        raise HTTPException(status_code=403, detail="admin privilege required")
    return resolved


@router.get("/staff/me")
async def staff_me(principal: PrincipalDep, session: DbDep) -> dict:
    """Return the calling employee's name, resolved from the VERIFIED token identity."""
    email = service.normalize_email(principal.claims.get("email"))
    phone = principal.claims.get("phone")
    try:
        phone = service.normalize_phone(phone)
    except ValueError:
        phone = None
    emp = await crud.find_employee_by_identity(session, email=email, phone=phone)
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return {
        "name": emp["name"],
        "full_name": f"{emp['name']} {emp['surname']}".strip(),
    }


# --- admin users management -------------------------------------------------


@router.get("/admin/users")
async def list_admin_users(_admin: UsersAdmin, session: DbDep) -> list[dict]:
    return await crud.list_admin_users(session)


@router.post("/admin/users")
async def create_admin_user(
    payload: AdminUserCreate, _admin: UsersAdmin, venue_id: VenueDep, session: DbDep
) -> dict:
    uid = (payload.uid or "").strip() or None
    email = service.normalize_email(payload.email)
    try:
        return await crud.create_admin_user(
            session,
            venue_id=venue_id,
            uid=uid,
            email=email,
            role=payload.role,
            allowed_pages=payload.allowed_pages,
            telegram_user_id=payload.telegram_user_id,
            venue_ids=payload.venue_ids,
        )
    except ValueError as exc:
        detail = str(exc)
        status = 409 if "already" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc


@router.put("/admin/users/{admin_id}")
async def update_admin_user(
    admin_id: int, payload: AdminUserUpdate, _admin: UsersAdmin, session: DbDep
) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    if "email" in fields:
        fields["email"] = service.normalize_email(fields["email"])
    if "uid" in fields:
        fields["uid"] = (fields["uid"] or "").strip() or None
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        updated = await crud.update_admin_user(session, admin_id=admin_id, fields=fields)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="admin user not found")
    return updated


@router.delete("/admin/users/{admin_id}")
async def delete_admin_user(admin_id: int, _admin: UsersAdmin, session: DbDep) -> dict:
    deleted = await crud.delete_admin_user(session, admin_id=admin_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="admin user not found")
    return {"ok": True}


@router.get("/admin/users/{admin_id}/employees")
async def list_managed_employees(
    admin_id: int, _admin: UsersAdmin, session: DbDep
) -> list[dict]:
    if await crud.get_admin_user(session, admin_id=admin_id) is None:
        raise HTTPException(status_code=404, detail="admin user not found")
    return await crud.list_managed_employees(session, manager_id=admin_id)


@router.put("/admin/users/{admin_id}/employees")
async def set_managed_employees(
    admin_id: int, payload: ManagerEmployeesSet, _admin: UsersAdmin, session: DbDep
) -> dict:
    result = await crud.set_managed_employees(
        session, manager_id=admin_id, employee_ids=payload.employee_ids
    )
    if result is None:
        raise HTTPException(status_code=404, detail="admin user not found")
    return {"ok": True, "employee_ids": result}


# --- employees --------------------------------------------------------------


@router.get("/employees")
async def list_employees(_admin: HrAdmin, session: DbDep) -> list[dict]:
    return await crud.list_employees(session)


def _employee_identity(
    payload: EmployeeCreate,
) -> tuple[str, str, str | None, str | None]:
    """Validate + normalize an employee payload's identity/pay; -> (name, surname, email, phone)."""
    name = (payload.name or "").strip()
    surname = (payload.surname or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    email = service.normalize_email(payload.email)
    try:
        phone = service.normalize_phone(payload.phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not email and not phone:
        raise HTTPException(
            status_code=400,
            detail="email or phone is required — staff log in with one of them",
        )
    try:
        service.validate_pay(payload.pay_type, payload.pay_rate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return name, surname, email, phone


@router.post("/employees", status_code=201)
async def create_employee(
    payload: EmployeeCreate, _admin: HrAdmin, venue_id: VenueDep, session: DbDep
) -> dict:
    name, surname, email, phone = _employee_identity(payload)
    try:
        return await crud.create_employee(
            session,
            venue_id=venue_id,
            name=name,
            surname=surname,
            email=email,
            phone=phone,
            is_stable_schedule=payload.is_stable_schedule,
            pay_type=payload.pay_type,
            pay_rate=payload.pay_rate,
            birth_date=payload.birth_date,
            default_role_id=payload.default_role_id,
            group_ids=payload.group_ids,
        )
    except ValueError as exc:
        detail = str(exc)
        status = 409 if "already belongs" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc


@router.put("/employees/{employee_id}")
async def update_employee(
    employee_id: int, payload: EmployeeCreate, _admin: HrAdmin, session: DbDep
) -> dict:
    name, surname, email, phone = _employee_identity(payload)
    try:
        updated = await crud.update_employee(
            session,
            employee_id=employee_id,
            name=name,
            surname=surname,
            email=email,
            phone=phone,
            is_stable_schedule=payload.is_stable_schedule,
            pay_type=payload.pay_type,
            pay_rate=payload.pay_rate,
            birth_date=payload.birth_date,
            default_role_id=payload.default_role_id,
            group_ids=payload.group_ids,
        )
    except ValueError as exc:
        detail = str(exc)
        status = 409 if "already belongs" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return updated


@router.put("/employees/{employee_id}/groups")
async def set_employee_groups(
    employee_id: int, payload: GroupsSet, _admin: HrAdmin, session: DbDep
) -> dict:
    try:
        result = await crud.set_employee_groups(
            session, employee_id=employee_id, group_ids=payload.group_ids
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return {"ok": True, "group_ids": result}


# --- job roles --------------------------------------------------------------


@router.get("/job-roles")
async def list_job_roles(_admin: RolesAdmin, session: DbDep) -> list[dict]:
    return await crud.list_job_roles(session)


@router.post("/job-roles")
async def create_job_role(
    payload: JobRoleCreate, _admin: RolesAdmin, venue_id: VenueDep, session: DbDep
) -> dict:
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    try:
        start_time = service.normalize_time(payload.start_time)
        end_time = service.normalize_time(payload.end_time)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await crud.create_job_role(
        session,
        venue_id=venue_id,
        name=name,
        start_time=start_time,
        end_time=end_time,
        color=payload.color,
    )


@router.put("/job-roles/{role_id}")
async def update_job_role(
    role_id: int, payload: JobRoleUpdate, _admin: RolesAdmin, session: DbDep
) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        if "start_time" in fields:
            fields["start_time"] = service.normalize_time(fields["start_time"])
        if "end_time" in fields:
            fields["end_time"] = service.normalize_time(fields["end_time"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    updated = await crud.update_job_role(session, role_id=role_id, fields=fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="role not found")
    return updated


# --- employee groups --------------------------------------------------------


@router.get("/employee-groups")
async def list_employee_groups(_admin: GroupsAdmin, session: DbDep) -> list[dict]:
    return await crud.list_employee_groups(session)


@router.post("/employee-groups")
async def create_employee_group(
    payload: EmployeeGroupCreate, _admin: GroupsAdmin, venue_id: VenueDep, session: DbDep
) -> dict:
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    return await crud.create_employee_group(
        session, venue_id=venue_id, name=name, color=payload.color
    )


@router.put("/employee-groups/{group_id}")
async def update_employee_group(
    group_id: int, payload: EmployeeGroupUpdate, _admin: GroupsAdmin, session: DbDep
) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    updated = await crud.update_employee_group(session, group_id=group_id, fields=fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="group not found")
    return updated


@router.get("/employee-groups/{group_id}/members")
async def list_group_members(
    group_id: int, _admin: GroupsAdmin, session: DbDep
) -> list[dict]:
    if await crud.get_employee_group(session, group_id=group_id) is None:
        raise HTTPException(status_code=404, detail="group not found")
    return await crud.list_group_members(session, group_id=group_id)


@router.put("/employee-groups/{group_id}/members")
async def set_group_members(
    group_id: int, payload: GroupMembersSet, _admin: GroupsAdmin, session: DbDep
) -> dict:
    result = await crud.set_group_members(
        session, group_id=group_id, employee_ids=payload.employee_ids
    )
    if result is None:
        raise HTTPException(status_code=404, detail="group not found")
    return {"ok": True, "employee_ids": result}


__all__ = ["router"]
