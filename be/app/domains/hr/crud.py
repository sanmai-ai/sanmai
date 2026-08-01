"""HR & access domain CRUD — ``text()`` SQL constants + async functions.

Covers employees, the admin_users RBAC trust anchor (+ per-venue grants + managed
employees), job roles, employee groups and their membership.

All SQL is dialect-agnostic (sqlite in tests, postgres in prod), mirroring
``be.app.domains.inventory.crud``:

* unqualified table names (no ``employees_stg.`` prefix);
* surrogate ids allocated app-side (``SELECT COALESCE(MAX(id),0)+1``);
* booleans stored as INTEGER 0/1; ``allowed_pages`` stored as JSON TEXT, parsed here;
* money bound as text through ``CAST(:x AS numeric)``;
* ``created_at``/``updated_at`` written as ISO-8601 strings from Python (no ``now()``);
* upserts / membership replaces are select-then-insert/update (no ``ON CONFLICT``);
* set membership uses an expanding IN bind (no postgres-only ``ANY(...)``).

VENUE SCOPING: employees / admin_users / job_roles / employee_groups are company-level
(they carry ``venue_id`` for uniformity but are not filtered by it); ``admin_user_venues``
is the venue-scoping seam consulted by :func:`be.app.deps.require_admin`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import RowMapping, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _to_bool(value: Any) -> bool:
    return bool(value)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _num_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _dump_pages(value: list[str] | None) -> str:
    return json.dumps([str(v) for v in (value or [])], ensure_ascii=False)


def _load_pages(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    return []


async def _next_id(session: AsyncSession, table: str) -> int:
    stmt = text(f"SELECT COALESCE(MAX(id), 0) + 1 AS n FROM {table}")  # noqa: S608 - fixed literal
    row = (await session.execute(stmt)).mappings().first()
    return int(row["n"]) if row is not None else 1


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #

_GET_EMPLOYEE = text("""
    SELECT id, name, surname, email, phone, is_stable_schedule, pay_type, pay_rate,
           birth_date, default_role_id, venue_id
    FROM employees
    WHERE id = :id
""")

_LIST_EMPLOYEES = text("""
    SELECT id, name, surname, email, phone, is_stable_schedule, pay_type, pay_rate,
           birth_date, default_role_id
    FROM employees
    ORDER BY name, surname, id
""")

_EMPLOYEE_BY_EMAIL = text("SELECT id FROM employees WHERE email = :email")
_EMPLOYEE_BY_PHONE = text("SELECT id FROM employees WHERE phone = :phone")

_FIND_BY_IDENTITY = text("""
    SELECT id, name, surname, email, phone
    FROM employees
    WHERE (CAST(:email AS TEXT) IS NOT NULL AND email = CAST(:email AS TEXT))
       OR (CAST(:phone AS TEXT) IS NOT NULL AND phone = CAST(:phone AS TEXT))
    ORDER BY id
""")

_INSERT_EMPLOYEE = text("""
    INSERT INTO employees
        (id, venue_id, name, surname, email, phone, is_stable_schedule, pay_type,
         pay_rate, birth_date, default_role_id, created_at, updated_at)
    VALUES
        (:id, :venue_id, :name, :surname, :email, :phone, :is_stable_schedule,
         :pay_type, CAST(:pay_rate AS numeric), :birth_date, :default_role_id,
         :created_at, :updated_at)
""")

_UPDATE_EMPLOYEE = text("""
    UPDATE employees SET
        name = :name, surname = :surname, email = :email, phone = :phone,
        is_stable_schedule = :is_stable_schedule, pay_type = :pay_type,
        pay_rate = CAST(:pay_rate AS numeric), birth_date = :birth_date,
        default_role_id = :default_role_id, updated_at = :updated_at
    WHERE id = :id
""")

_GROUPS_FOR_EMPLOYEES = text("""
    SELECT m.employee_id, g.id AS group_id, g.name, g.color
    FROM employee_group_members m
    JOIN employee_groups g ON g.id = m.group_id AND g.is_active = 1
    ORDER BY g.name
""")

_ROLE_NAMES = text("SELECT id, name FROM job_roles")

_EXISTING_ROLE = text("SELECT id FROM job_roles WHERE id = :id")

_EXISTING_GROUPS = text(
    "SELECT id FROM employee_groups WHERE id IN :ids"
).bindparams(bindparam("ids", expanding=True))

_DELETE_EMPLOYEE_GROUPS = text(
    "DELETE FROM employee_group_members WHERE employee_id = :employee_id"
)

_INSERT_EMPLOYEE_GROUP = text(
    "INSERT INTO employee_group_members (employee_id, group_id) VALUES (:employee_id, :group_id)"
)


def _row_to_employee(m: RowMapping) -> dict:
    return {
        "id": m["id"],
        "name": m["name"],
        "surname": m["surname"],
        "email": m["email"],
        "phone": m["phone"],
        "is_stable_schedule": _to_bool(m["is_stable_schedule"]),
        "pay_type": m["pay_type"],
        "pay_rate": _num(m["pay_rate"]),
        "birth_date": m["birth_date"],
        "default_role_id": m["default_role_id"],
    }


async def get_employee(session: AsyncSession, *, employee_id: int) -> dict | None:
    row = (await session.execute(_GET_EMPLOYEE, {"id": employee_id})).mappings().first()
    return _row_to_employee(row) if row is not None else None


async def find_employee_by_identity(
    session: AsyncSession, *, email: str | None, phone: str | None
) -> dict | None:
    """Resolve an employee by verified email/phone identity. ``None`` if absent."""
    if not email and not phone:
        return None
    row = (
        await session.execute(_FIND_BY_IDENTITY, {"email": email, "phone": phone})
    ).mappings().first()
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "surname": row["surname"],
        "email": row["email"],
        "phone": row["phone"],
    }


async def list_employees(session: AsyncSession) -> list[dict]:
    """Employees with their group ids/names and default-role name (no N+1)."""
    rows = (await session.execute(_LIST_EMPLOYEES)).mappings().all()
    group_rows = (await session.execute(_GROUPS_FOR_EMPLOYEES)).mappings().all()
    role_rows = (await session.execute(_ROLE_NAMES)).mappings().all()
    role_names = {r["id"]: r["name"] for r in role_rows}

    groups_by_emp: dict[int, list[dict]] = {}
    for g in group_rows:
        groups_by_emp.setdefault(g["employee_id"], []).append(
            {"id": g["group_id"], "name": g["name"], "color": g["color"]}
        )

    out: list[dict] = []
    for r in rows:
        emp = _row_to_employee(r)
        groups = groups_by_emp.get(r["id"], [])
        emp["default_role_name"] = role_names.get(r["default_role_id"])
        emp["groups"] = groups
        emp["group_ids"] = [g["id"] for g in groups]
        out.append(emp)
    return out


async def _assert_identity_free(
    session: AsyncSession,
    *,
    email: str | None,
    phone: str | None,
    exclude_id: int | None,
) -> None:
    if email:
        row = (await session.execute(_EMPLOYEE_BY_EMAIL, {"email": email})).mappings().first()
        if row is not None and row["id"] != exclude_id:
            raise ValueError(f"email already belongs to employee #{row['id']}")
    if phone:
        row = (await session.execute(_EMPLOYEE_BY_PHONE, {"phone": phone})).mappings().first()
        if row is not None and row["id"] != exclude_id:
            raise ValueError(f"phone already belongs to employee #{row['id']}")


async def _validate_role_and_groups(
    session: AsyncSession, *, default_role_id: int | None, group_ids: list[int]
) -> list[int]:
    if default_role_id is not None:
        role = (await session.execute(_EXISTING_ROLE, {"id": default_role_id})).first()
        if role is None:
            raise ValueError(f"unknown role id: {default_role_id}")
    deduped = list(dict.fromkeys(group_ids))
    if deduped:
        valid = (
            await session.execute(_EXISTING_GROUPS, {"ids": deduped})
        ).mappings().all()
        missing = set(deduped) - {r["id"] for r in valid}
        if missing:
            raise ValueError(f"unknown group id(s): {sorted(missing)}")
    return deduped


async def create_employee(
    session: AsyncSession,
    *,
    venue_id: str,
    name: str,
    surname: str,
    email: str | None,
    phone: str | None,
    is_stable_schedule: bool,
    pay_type: str,
    pay_rate: float | None,
    birth_date: str | None,
    default_role_id: int | None,
    group_ids: list[int],
) -> dict:
    """Create an employee + group membership. Raises ValueError on bad/duplicate input."""
    if not name:
        raise ValueError("name is required")
    if not email and not phone:
        raise ValueError("email or phone is required — staff log in with one of them")
    await _assert_identity_free(session, email=email, phone=phone, exclude_id=None)
    deduped = await _validate_role_and_groups(
        session, default_role_id=default_role_id, group_ids=group_ids
    )

    new_id = await _next_id(session, "employees")
    now = _now_iso()
    await session.execute(
        _INSERT_EMPLOYEE,
        {
            "id": new_id,
            "venue_id": venue_id,
            "name": name,
            "surname": surname,
            "email": email,
            "phone": phone,
            "is_stable_schedule": 1 if is_stable_schedule else 0,
            "pay_type": pay_type,
            "pay_rate": _num_str(pay_rate),
            "birth_date": birth_date,
            "default_role_id": default_role_id,
            "created_at": now,
            "updated_at": now,
        },
    )
    for gid in deduped:
        await session.execute(
            _INSERT_EMPLOYEE_GROUP, {"employee_id": new_id, "group_id": gid}
        )
    await session.commit()
    created = await get_employee(session, employee_id=new_id)
    assert created is not None
    created["group_ids"] = deduped
    return created


async def update_employee(
    session: AsyncSession,
    *,
    employee_id: int,
    name: str,
    surname: str,
    email: str | None,
    phone: str | None,
    is_stable_schedule: bool,
    pay_type: str,
    pay_rate: float | None,
    birth_date: str | None,
    default_role_id: int | None,
    group_ids: list[int],
) -> dict | None:
    """Full profile update + replace-all group membership. ``None`` if id absent."""
    if await get_employee(session, employee_id=employee_id) is None:
        return None
    if not name:
        raise ValueError("name is required")
    if not email and not phone:
        raise ValueError("email or phone is required — staff log in with one of them")
    await _assert_identity_free(session, email=email, phone=phone, exclude_id=employee_id)
    deduped = await _validate_role_and_groups(
        session, default_role_id=default_role_id, group_ids=group_ids
    )

    await session.execute(
        _UPDATE_EMPLOYEE,
        {
            "id": employee_id,
            "name": name,
            "surname": surname,
            "email": email,
            "phone": phone,
            "is_stable_schedule": 1 if is_stable_schedule else 0,
            "pay_type": pay_type,
            "pay_rate": _num_str(pay_rate),
            "birth_date": birth_date,
            "default_role_id": default_role_id,
            "updated_at": _now_iso(),
        },
    )
    await session.execute(_DELETE_EMPLOYEE_GROUPS, {"employee_id": employee_id})
    for gid in deduped:
        await session.execute(
            _INSERT_EMPLOYEE_GROUP, {"employee_id": employee_id, "group_id": gid}
        )
    await session.commit()
    updated = await get_employee(session, employee_id=employee_id)
    assert updated is not None
    updated["group_ids"] = deduped
    return updated


async def set_employee_groups(
    session: AsyncSession, *, employee_id: int, group_ids: list[int]
) -> list[int] | None:
    """Replace-all write of an employee's group membership. ``None`` if id absent."""
    if await get_employee(session, employee_id=employee_id) is None:
        return None
    deduped = await _validate_role_and_groups(
        session, default_role_id=None, group_ids=group_ids
    )
    await session.execute(_DELETE_EMPLOYEE_GROUPS, {"employee_id": employee_id})
    for gid in deduped:
        await session.execute(
            _INSERT_EMPLOYEE_GROUP, {"employee_id": employee_id, "group_id": gid}
        )
    await session.commit()
    return deduped


# --------------------------------------------------------------------------- #
# Admin users (RBAC trust anchor)
# --------------------------------------------------------------------------- #

_LIST_ADMIN_USERS = text("""
    SELECT id, uid, email, role, allowed_pages, is_active, telegram_user_id, created_at
    FROM admin_users
    ORDER BY created_at, id
""")

_GET_ADMIN_USER = text("""
    SELECT id, uid, email, role, allowed_pages, is_active, telegram_user_id, created_at
    FROM admin_users
    WHERE id = :id
""")

_ADMIN_BY_EMAIL = text("SELECT id FROM admin_users WHERE email = :email")
_ADMIN_BY_UID = text("SELECT id FROM admin_users WHERE uid = :uid")
_ADMIN_BY_TELEGRAM = text(
    "SELECT id FROM admin_users WHERE telegram_user_id = :telegram_user_id"
)

_INSERT_ADMIN_USER = text("""
    INSERT INTO admin_users
        (id, venue_id, uid, email, role, allowed_pages, is_active, telegram_user_id,
         created_at)
    VALUES
        (:id, :venue_id, :uid, :email, :role, :allowed_pages, :is_active,
         :telegram_user_id, :created_at)
""")

_DELETE_ADMIN_USER = text("DELETE FROM admin_users WHERE id = :id")
_DELETE_ADMIN_VENUES = text(
    "DELETE FROM admin_user_venues WHERE admin_user_id = :admin_user_id"
)
_INSERT_ADMIN_VENUE = text(
    "INSERT INTO admin_user_venues (admin_user_id, venue_id) VALUES (:admin_user_id, :venue_id)"
)
_ADMIN_VENUES = text(
    "SELECT venue_id FROM admin_user_venues WHERE admin_user_id = :admin_user_id ORDER BY venue_id"
)

_RESOLVE_ADMIN = text("""
    SELECT id, uid, email, role, allowed_pages
    FROM admin_users
    WHERE is_active = 1
      AND (
        (CAST(:uid AS TEXT) IS NOT NULL AND uid = CAST(:uid AS TEXT))
        OR (CAST(:email AS TEXT) IS NOT NULL AND email = CAST(:email AS TEXT))
      )
    ORDER BY id
""")

_MANAGED_EMPLOYEES = text("""
    SELECT e.id, e.name, e.surname
    FROM manager_employees me
    JOIN employees e ON e.id = me.employee_id
    WHERE me.manager_id = :manager_id
    ORDER BY e.name, e.surname
""")

_MANAGED_EMPLOYEE_IDS = text(
    "SELECT employee_id FROM manager_employees WHERE manager_id = :manager_id ORDER BY employee_id"
)
_DELETE_MANAGED = text("DELETE FROM manager_employees WHERE manager_id = :manager_id")
_INSERT_MANAGED = text(
    "INSERT INTO manager_employees (manager_id, employee_id) VALUES (:manager_id, :employee_id)"
)


def _row_to_admin_user(m: RowMapping) -> dict:
    return {
        "id": m["id"],
        "uid": m["uid"],
        "email": m["email"],
        "role": m["role"],
        "allowed_pages": _load_pages(m["allowed_pages"]),
        "is_active": _to_bool(m["is_active"]),
        "telegram_user_id": m["telegram_user_id"],
        "created_at": m["created_at"],
    }


async def _admin_with_venues(session: AsyncSession, admin_id: int) -> dict | None:
    row = (await session.execute(_GET_ADMIN_USER, {"id": admin_id})).mappings().first()
    if row is None:
        return None
    out = _row_to_admin_user(row)
    venues = (await session.execute(_ADMIN_VENUES, {"admin_user_id": admin_id})).mappings().all()
    out["venue_ids"] = [v["venue_id"] for v in venues]
    return out


async def list_admin_users(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(_LIST_ADMIN_USERS)).mappings().all()
    out = []
    for r in rows:
        item = _row_to_admin_user(r)
        venues = (
            await session.execute(_ADMIN_VENUES, {"admin_user_id": r["id"]})
        ).mappings().all()
        item["venue_ids"] = [v["venue_id"] for v in venues]
        out.append(item)
    return out


async def get_admin_user(session: AsyncSession, *, admin_id: int) -> dict | None:
    return await _admin_with_venues(session, admin_id)


async def create_admin_user(
    session: AsyncSession,
    *,
    venue_id: str,
    uid: str | None,
    email: str | None,
    role: str,
    allowed_pages: list[str],
    telegram_user_id: int | None,
    venue_ids: list[str],
) -> dict:
    """Create an admin_users row (+ venue grants). Raises ValueError on bad/duplicate input."""
    if not uid and not email:
        raise ValueError("uid or email is required — it is the admin's login identity")
    if email:
        dup = (await session.execute(_ADMIN_BY_EMAIL, {"email": email})).first()
        if dup is not None:
            raise ValueError("that email is already an admin user")
    if uid:
        dup = (await session.execute(_ADMIN_BY_UID, {"uid": uid})).first()
        if dup is not None:
            raise ValueError("that uid is already an admin user")
    if telegram_user_id is not None:
        dup = (
            await session.execute(_ADMIN_BY_TELEGRAM, {"telegram_user_id": telegram_user_id})
        ).first()
        if dup is not None:
            raise ValueError("that telegram id is already linked to another admin user")

    new_id = await _next_id(session, "admin_users")
    await session.execute(
        _INSERT_ADMIN_USER,
        {
            "id": new_id,
            "venue_id": venue_id,
            "uid": uid,
            "email": email,
            "role": role,
            "allowed_pages": _dump_pages(allowed_pages),
            "is_active": 1,
            "telegram_user_id": telegram_user_id,
            "created_at": _now_iso(),
        },
    )
    for v in dict.fromkeys(venue_ids):
        await session.execute(
            _INSERT_ADMIN_VENUE, {"admin_user_id": new_id, "venue_id": v}
        )
    await session.commit()
    created = await _admin_with_venues(session, new_id)
    assert created is not None
    return created


async def update_admin_user(
    session: AsyncSession, *, admin_id: int, fields: dict[str, Any]
) -> dict | None:
    """Merge provided *fields* into an admin_users row. ``None`` if id absent.

    Read-merge-write keeps the statement portable and injection-safe. ``venue_ids`` (when
    present) replaces the whole venue-grant set; ``telegram_user_id`` present-and-null clears.
    """
    current = await _admin_with_venues(session, admin_id)
    if current is None:
        return None

    role = fields.get("role", current["role"]) if fields.get("role") is not None else current["role"]
    allowed = (
        fields["allowed_pages"]
        if fields.get("allowed_pages") is not None
        else current["allowed_pages"]
    )
    is_active = (
        bool(fields["is_active"]) if fields.get("is_active") is not None else current["is_active"]
    )
    uid = fields["uid"] if "uid" in fields else current["uid"]
    email = fields["email"] if "email" in fields else current["email"]
    telegram = (
        fields["telegram_user_id"]
        if "telegram_user_id" in fields
        else current["telegram_user_id"]
    )

    if email and email != current["email"]:
        dup = (await session.execute(_ADMIN_BY_EMAIL, {"email": email})).mappings().first()
        if dup is not None and dup["id"] != admin_id:
            raise ValueError("that email is already an admin user")
    if uid and uid != current["uid"]:
        dup = (await session.execute(_ADMIN_BY_UID, {"uid": uid})).mappings().first()
        if dup is not None and dup["id"] != admin_id:
            raise ValueError("that uid is already an admin user")
    if telegram is not None and telegram != current["telegram_user_id"]:
        dup = (
            await session.execute(_ADMIN_BY_TELEGRAM, {"telegram_user_id": telegram})
        ).mappings().first()
        if dup is not None and dup["id"] != admin_id:
            raise ValueError("that telegram id is already linked to another admin user")

    await session.execute(
        text("""
            UPDATE admin_users SET
                uid = :uid, email = :email, role = :role,
                allowed_pages = :allowed_pages, is_active = :is_active,
                telegram_user_id = :telegram_user_id
            WHERE id = :id
        """),
        {
            "id": admin_id,
            "uid": uid,
            "email": email,
            "role": role,
            "allowed_pages": _dump_pages(allowed),
            "is_active": 1 if is_active else 0,
            "telegram_user_id": telegram,
        },
    )
    if "venue_ids" in fields and fields["venue_ids"] is not None:
        await session.execute(_DELETE_ADMIN_VENUES, {"admin_user_id": admin_id})
        for v in dict.fromkeys(fields["venue_ids"]):
            await session.execute(
                _INSERT_ADMIN_VENUE, {"admin_user_id": admin_id, "venue_id": v}
            )
    await session.commit()
    return await _admin_with_venues(session, admin_id)


async def delete_admin_user(session: AsyncSession, *, admin_id: int) -> bool:
    if await _admin_with_venues(session, admin_id) is None:
        return False
    await session.execute(_DELETE_ADMIN_VENUES, {"admin_user_id": admin_id})
    await session.execute(_DELETE_MANAGED, {"manager_id": admin_id})
    await session.execute(_DELETE_ADMIN_USER, {"id": admin_id})
    await session.commit()
    return True


async def resolve_admin_access(
    session: AsyncSession, *, uid: str | None, email: str | None
) -> dict | None:
    """Resolve the caller's admin role/pages/managed-employees/venues, or ``None``."""
    row = (
        await session.execute(_RESOLVE_ADMIN, {"uid": uid, "email": email})
    ).mappings().first()
    if row is None:
        return None
    managed_ids: list[int] = []
    if row["role"] == "manager":
        managed = (
            await session.execute(_MANAGED_EMPLOYEE_IDS, {"manager_id": row["id"]})
        ).mappings().all()
        managed_ids = [m["employee_id"] for m in managed]
    venues = (
        await session.execute(_ADMIN_VENUES, {"admin_user_id": row["id"]})
    ).mappings().all()
    return {
        "role": row["role"],
        "allowed_pages": _load_pages(row["allowed_pages"]),
        "managed_employee_ids": managed_ids,
        "venue_ids": [v["venue_id"] for v in venues],
    }


async def list_managed_employees(session: AsyncSession, *, manager_id: int) -> list[dict]:
    rows = (await session.execute(_MANAGED_EMPLOYEES, {"manager_id": manager_id})).mappings().all()
    return [
        {"id": r["id"], "name": r["name"], "surname": r["surname"]} for r in rows
    ]


async def set_managed_employees(
    session: AsyncSession, *, manager_id: int, employee_ids: list[int]
) -> list[int] | None:
    """Replace-all write of a manager's employee assignments. ``None`` if manager absent."""
    if await _admin_with_venues(session, manager_id) is None:
        return None
    await session.execute(_DELETE_MANAGED, {"manager_id": manager_id})
    deduped = list(dict.fromkeys(employee_ids))
    for eid in deduped:
        await session.execute(
            _INSERT_MANAGED, {"manager_id": manager_id, "employee_id": eid}
        )
    await session.commit()
    return deduped


# --------------------------------------------------------------------------- #
# Job roles
# --------------------------------------------------------------------------- #

_LIST_ROLES = text("""
    SELECT id, name, start_time, end_time, color, is_active
    FROM job_roles
    ORDER BY name, id
""")

_GET_ROLE = text("""
    SELECT id, name, start_time, end_time, color, is_active
    FROM job_roles
    WHERE id = :id
""")

_INSERT_ROLE = text("""
    INSERT INTO job_roles (id, venue_id, name, start_time, end_time, color, is_active, created_at)
    VALUES (:id, :venue_id, :name, :start_time, :end_time, :color, 1, :created_at)
""")


def _row_to_role(m: RowMapping) -> dict:
    return {
        "id": m["id"],
        "name": m["name"],
        "start_time": m["start_time"],
        "end_time": m["end_time"],
        "color": m["color"],
        "is_active": _to_bool(m["is_active"]),
    }


async def list_job_roles(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(_LIST_ROLES)).mappings().all()
    return [_row_to_role(r) for r in rows]


async def get_job_role(session: AsyncSession, *, role_id: int) -> dict | None:
    row = (await session.execute(_GET_ROLE, {"id": role_id})).mappings().first()
    return _row_to_role(row) if row is not None else None


async def create_job_role(
    session: AsyncSession,
    *,
    venue_id: str,
    name: str,
    start_time: str | None,
    end_time: str | None,
    color: str,
) -> dict:
    if not name:
        raise ValueError("name is required")
    new_id = await _next_id(session, "job_roles")
    await session.execute(
        _INSERT_ROLE,
        {
            "id": new_id,
            "venue_id": venue_id,
            "name": name,
            "start_time": start_time,
            "end_time": end_time,
            "color": color or "#60a5fa",
            "created_at": _now_iso(),
        },
    )
    await session.commit()
    created = await get_job_role(session, role_id=new_id)
    assert created is not None
    return created


async def update_job_role(
    session: AsyncSession, *, role_id: int, fields: dict[str, Any]
) -> dict | None:
    current = await get_job_role(session, role_id=role_id)
    if current is None:
        return None
    name = fields["name"] if fields.get("name") is not None else current["name"]
    start_time = fields["start_time"] if "start_time" in fields else current["start_time"]
    end_time = fields["end_time"] if "end_time" in fields else current["end_time"]
    color = fields["color"] if fields.get("color") is not None else current["color"]
    is_active = (
        bool(fields["is_active"]) if fields.get("is_active") is not None else current["is_active"]
    )
    await session.execute(
        text("""
            UPDATE job_roles SET
                name = :name, start_time = :start_time, end_time = :end_time,
                color = :color, is_active = :is_active
            WHERE id = :id
        """),
        {
            "id": role_id,
            "name": name,
            "start_time": start_time,
            "end_time": end_time,
            "color": color,
            "is_active": 1 if is_active else 0,
        },
    )
    await session.commit()
    return await get_job_role(session, role_id=role_id)


# --------------------------------------------------------------------------- #
# Employee groups
# --------------------------------------------------------------------------- #

_LIST_GROUPS = text("""
    SELECT g.id, g.name, g.color, g.is_active,
           (SELECT COUNT(*) FROM employee_group_members m WHERE m.group_id = g.id) AS member_count
    FROM employee_groups g
    ORDER BY g.name, g.id
""")

_GET_GROUP = text(
    "SELECT id, name, color, is_active FROM employee_groups WHERE id = :id"
)

_INSERT_GROUP = text("""
    INSERT INTO employee_groups (id, venue_id, name, color, is_active, created_at)
    VALUES (:id, :venue_id, :name, :color, 1, :created_at)
""")

_GROUP_MEMBERS = text("""
    SELECT e.id, e.name, e.surname
    FROM employee_group_members m
    JOIN employees e ON e.id = m.employee_id
    WHERE m.group_id = :group_id
    ORDER BY e.name, e.surname
""")

_DELETE_GROUP_MEMBERS = text(
    "DELETE FROM employee_group_members WHERE group_id = :group_id"
)
_INSERT_GROUP_MEMBER = text(
    "INSERT INTO employee_group_members (employee_id, group_id) VALUES (:employee_id, :group_id)"
)


def _row_to_group(m: RowMapping) -> dict:
    return {
        "id": m["id"],
        "name": m["name"],
        "color": m["color"],
        "is_active": _to_bool(m["is_active"]),
    }


async def list_employee_groups(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(_LIST_GROUPS)).mappings().all()
    out = []
    for r in rows:
        item = _row_to_group(r)
        item["member_count"] = int(r["member_count"])
        out.append(item)
    return out


async def get_employee_group(session: AsyncSession, *, group_id: int) -> dict | None:
    row = (await session.execute(_GET_GROUP, {"id": group_id})).mappings().first()
    return _row_to_group(row) if row is not None else None


async def create_employee_group(
    session: AsyncSession, *, venue_id: str, name: str, color: str
) -> dict:
    if not name:
        raise ValueError("name is required")
    new_id = await _next_id(session, "employee_groups")
    await session.execute(
        _INSERT_GROUP,
        {
            "id": new_id,
            "venue_id": venue_id,
            "name": name,
            "color": color or "#60a5fa",
            "created_at": _now_iso(),
        },
    )
    await session.commit()
    created = await get_employee_group(session, group_id=new_id)
    assert created is not None
    return created


async def update_employee_group(
    session: AsyncSession, *, group_id: int, fields: dict[str, Any]
) -> dict | None:
    current = await get_employee_group(session, group_id=group_id)
    if current is None:
        return None
    name = fields["name"] if fields.get("name") is not None else current["name"]
    color = fields["color"] if fields.get("color") is not None else current["color"]
    is_active = (
        bool(fields["is_active"]) if fields.get("is_active") is not None else current["is_active"]
    )
    await session.execute(
        text("""
            UPDATE employee_groups SET name = :name, color = :color, is_active = :is_active
            WHERE id = :id
        """),
        {
            "id": group_id,
            "name": name,
            "color": color,
            "is_active": 1 if is_active else 0,
        },
    )
    await session.commit()
    return await get_employee_group(session, group_id=group_id)


async def list_group_members(session: AsyncSession, *, group_id: int) -> list[dict]:
    rows = (await session.execute(_GROUP_MEMBERS, {"group_id": group_id})).mappings().all()
    return [
        {"id": r["id"], "name": f"{r['name']} {r['surname']}".strip()} for r in rows
    ]


async def set_group_members(
    session: AsyncSession, *, group_id: int, employee_ids: list[int]
) -> list[int] | None:
    """Replace-all write of a group's member list. ``None`` if the group is absent."""
    if await get_employee_group(session, group_id=group_id) is None:
        return None
    await session.execute(_DELETE_GROUP_MEMBERS, {"group_id": group_id})
    deduped = list(dict.fromkeys(employee_ids))
    for eid in deduped:
        await session.execute(
            _INSERT_GROUP_MEMBER, {"employee_id": eid, "group_id": group_id}
        )
    await session.commit()
    return deduped


__all__ = [
    "get_employee",
    "find_employee_by_identity",
    "list_employees",
    "create_employee",
    "update_employee",
    "set_employee_groups",
    "list_admin_users",
    "get_admin_user",
    "create_admin_user",
    "update_admin_user",
    "delete_admin_user",
    "resolve_admin_access",
    "list_managed_employees",
    "set_managed_employees",
    "list_job_roles",
    "get_job_role",
    "create_job_role",
    "update_job_role",
    "list_employee_groups",
    "get_employee_group",
    "create_employee_group",
    "update_employee_group",
    "list_group_members",
    "set_group_members",
]
