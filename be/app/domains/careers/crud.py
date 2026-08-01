"""Careers CRUD — ``text()`` SQL constants + async functions.

All SQL is dialect-agnostic (sqlite in tests, postgres in prod), mirroring the
suggestions/hr/tasks domains:

* unqualified table names; surrogate ids allocated app-side (``_next_id``);
* booleans stored as INTEGER 0/1; timestamps as TEXT ISO-8601 (no ``now()``);
* string arrays (responsibilities/requirements) stored as JSON-as-TEXT and
  serialized/parsed here (portable — no postgres-only jsonb);
* nullable TEXT filters use ``CAST(:p AS TEXT) IS NULL OR col = CAST(:p AS TEXT)``.

PII: application rows are all-PII; the router only ever returns them behind
``require_admin("careers")``. Position rows carry no PII.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _b(value: Any) -> bool:
    return bool(value)


async def _next_id(session: AsyncSession, table: str) -> int:
    stmt = text(f"SELECT COALESCE(MAX(id), 0) + 1 AS n FROM {table}")  # noqa: S608 - fixed literal
    row = (await session.execute(stmt)).mappings().first()
    return int(row["n"]) if row is not None else 1


def _loads(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    return []


def _position_row(r: Any) -> dict:
    return {
        "id": r["id"],
        "venue_id": r["venue_id"],
        "department": r["department"],
        "work_mode": r["work_mode"],
        "title_en": r["title_en"],
        "title_he": r["title_he"],
        "location_en": r["location_en"],
        "location_he": r["location_he"],
        "salary_en": r["salary_en"],
        "salary_he": r["salary_he"],
        "summary_en": r["summary_en"],
        "summary_he": r["summary_he"],
        "responsibilities_en": _loads(r["responsibilities_en"]),
        "responsibilities_he": _loads(r["responsibilities_he"]),
        "requirements_en": _loads(r["requirements_en"]),
        "requirements_he": _loads(r["requirements_he"]),
        "sort_order": int(r["sort_order"]),
        "is_active": _b(r["is_active"]),
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


# --------------------------------------------------------------------------- #
# Positions
# --------------------------------------------------------------------------- #

_POSITION_COLS = (
    "id, venue_id, department, work_mode, title_en, title_he, "
    "location_en, location_he, salary_en, salary_he, summary_en, summary_he, "
    "responsibilities_en, responsibilities_he, requirements_en, requirements_he, "
    "sort_order, is_active, created_at, updated_at"
)

_LIST_POSITIONS = text(
    f"""
    SELECT {_POSITION_COLS}
    FROM careers_positions
    WHERE (CAST(:active_only AS INTEGER) = 0 OR is_active = 1)
    ORDER BY sort_order ASC, id ASC
    """  # noqa: S608 - _POSITION_COLS is a fixed literal
)

_GET_POSITION = text(f"SELECT {_POSITION_COLS} FROM careers_positions WHERE id = :id")  # noqa: S608

_INSERT_POSITION = text(
    """
    INSERT INTO careers_positions (
        id, venue_id, department, work_mode, title_en, title_he,
        location_en, location_he, salary_en, salary_he, summary_en, summary_he,
        responsibilities_en, responsibilities_he, requirements_en, requirements_he,
        sort_order, is_active, created_at, updated_at
    ) VALUES (
        :id, :venue_id, :department, :work_mode, :title_en, :title_he,
        :location_en, :location_he, :salary_en, :salary_he, :summary_en, :summary_he,
        :responsibilities_en, :responsibilities_he, :requirements_en, :requirements_he,
        :sort_order, :is_active, :created_at, :created_at
    )
    """
)

_DELETE_POSITION = text("DELETE FROM careers_positions WHERE id = :id")

_TOGGLE_POSITION = text(
    "UPDATE careers_positions SET is_active = :active, updated_at = :now WHERE id = :id"
)


async def list_positions(
    session: AsyncSession, *, active_only: bool
) -> list[dict]:
    rows = (
        await session.execute(_LIST_POSITIONS, {"active_only": 1 if active_only else 0})
    ).mappings().all()
    return [_position_row(r) for r in rows]


async def get_position(session: AsyncSession, *, position_id: int) -> dict | None:
    row = (await session.execute(_GET_POSITION, {"id": position_id})).mappings().first()
    return _position_row(row) if row is not None else None


async def create_position(
    session: AsyncSession, *, venue_id: str, values: dict[str, Any]
) -> dict:
    new_id = await _next_id(session, "careers_positions")
    now = _now_iso()
    await session.execute(
        _INSERT_POSITION,
        {
            "id": new_id,
            "venue_id": venue_id,
            "department": values["department"],
            "work_mode": values["work_mode"],
            "title_en": values["title_en"],
            "title_he": values["title_he"],
            "location_en": values["location_en"],
            "location_he": values["location_he"],
            "salary_en": values["salary_en"],
            "salary_he": values["salary_he"],
            "summary_en": values["summary_en"],
            "summary_he": values["summary_he"],
            "responsibilities_en": json.dumps(values["responsibilities_en"]),
            "responsibilities_he": json.dumps(values["responsibilities_he"]),
            "requirements_en": json.dumps(values["requirements_en"]),
            "requirements_he": json.dumps(values["requirements_he"]),
            "sort_order": values["sort_order"],
            "is_active": 1 if values["is_active"] else 0,
            "created_at": now,
        },
    )
    await session.commit()
    got = await get_position(session, position_id=new_id)
    assert got is not None  # noqa: S101 - just inserted
    return got


# columns that map straight through on update (value coercion handled in the router)
_POSITION_TEXT_COLS = (
    "department",
    "work_mode",
    "title_en",
    "title_he",
    "location_en",
    "location_he",
    "salary_en",
    "salary_he",
    "summary_en",
    "summary_he",
)
_POSITION_JSON_COLS = (
    "responsibilities_en",
    "responsibilities_he",
    "requirements_en",
    "requirements_he",
)


async def update_position(
    session: AsyncSession, *, position_id: int, changes: dict[str, Any]
) -> dict | None:
    """Apply a partial update (only provided keys). Returns the fresh row, or None."""
    if await get_position(session, position_id=position_id) is None:
        return None
    sets: list[str] = []
    params: dict[str, Any] = {"id": position_id, "now": _now_iso()}
    for col in _POSITION_TEXT_COLS:
        if col in changes:
            sets.append(f"{col} = :{col}")
            params[col] = changes[col]
    for col in _POSITION_JSON_COLS:
        if col in changes:
            sets.append(f"{col} = :{col}")
            params[col] = json.dumps(changes[col])
    if "sort_order" in changes:
        sets.append("sort_order = :sort_order")
        params["sort_order"] = changes["sort_order"]
    if "is_active" in changes:
        sets.append("is_active = :is_active")
        params["is_active"] = 1 if changes["is_active"] else 0
    if sets:
        sets.append("updated_at = :now")
        stmt = text(  # noqa: S608 - column names are from the fixed allow-lists above
            f"UPDATE careers_positions SET {', '.join(sets)} WHERE id = :id"
        )
        await session.execute(stmt, params)
        await session.commit()
    return await get_position(session, position_id=position_id)


async def set_position_active(
    session: AsyncSession, *, position_id: int, active: bool
) -> dict | None:
    if await get_position(session, position_id=position_id) is None:
        return None
    await session.execute(
        _TOGGLE_POSITION,
        {"id": position_id, "active": 1 if active else 0, "now": _now_iso()},
    )
    await session.commit()
    return await get_position(session, position_id=position_id)


async def delete_position(session: AsyncSession, *, position_id: int) -> bool:
    if await get_position(session, position_id=position_id) is None:
        return False
    await session.execute(_DELETE_POSITION, {"id": position_id})
    await session.commit()
    return True


# --------------------------------------------------------------------------- #
# Applications (all-PII — admin reads only)
# --------------------------------------------------------------------------- #

_APPLICATION_COLS = (
    "id, venue_id, position_id, position_title_en, position_title_he, "
    "full_name, email, phone, city, street, experience, start_date, "
    "citizenship, english, lang, cv_key, status, created_at"
)

_INSERT_APPLICATION = text(
    """
    INSERT INTO careers_applications (
        id, venue_id, position_id, position_title_en, position_title_he,
        full_name, email, phone, city, street, experience, start_date,
        citizenship, english, lang, cv_key, status, created_at
    ) VALUES (
        :id, :venue_id, :position_id, :position_title_en, :position_title_he,
        :full_name, :email, :phone, :city, :street, :experience, :start_date,
        :citizenship, :english, :lang, NULL, 'new', :created_at
    )
    """
)

_LIST_APPLICATIONS = text(
    f"""
    SELECT {_APPLICATION_COLS}
    FROM careers_applications
    WHERE (CAST(:status_filter AS TEXT) IS NULL
           OR status = CAST(:status_filter AS TEXT))
    ORDER BY created_at DESC, id DESC
    """  # noqa: S608 - _APPLICATION_COLS is a fixed literal
)

_GET_APPLICATION = text(
    f"SELECT {_APPLICATION_COLS} FROM careers_applications WHERE id = :id"  # noqa: S608
)

_SET_APPLICATION_STATUS = text(
    "UPDATE careers_applications SET status = :status WHERE id = :id"
)

_SET_APPLICATION_CV = text(
    "UPDATE careers_applications SET cv_key = :cv_key WHERE id = :id"
)

_DELETE_APPLICATION = text("DELETE FROM careers_applications WHERE id = :id")


def _application_row(r: Any) -> dict:
    return {
        "id": r["id"],
        "venue_id": r["venue_id"],
        "position_id": r["position_id"],
        "position_title_en": r["position_title_en"],
        "position_title_he": r["position_title_he"],
        "full_name": r["full_name"],
        "email": r["email"],
        "phone": r["phone"],
        "city": r["city"],
        "street": r["street"],
        "experience": r["experience"],
        "start_date": r["start_date"],
        "citizenship": _b(r["citizenship"]),
        "english": _b(r["english"]),
        "lang": r["lang"],
        "cv_key": r["cv_key"],
        "status": r["status"],
        "created_at": r["created_at"],
    }


async def create_application(
    session: AsyncSession,
    *,
    venue_id: str,
    position_id: int | None,
    position_title_en: str,
    position_title_he: str,
    fields: dict[str, Any],
) -> dict:
    new_id = await _next_id(session, "careers_applications")
    now = _now_iso()
    await session.execute(
        _INSERT_APPLICATION,
        {
            "id": new_id,
            "venue_id": venue_id,
            "position_id": position_id,
            "position_title_en": position_title_en,
            "position_title_he": position_title_he,
            "full_name": fields["full_name"],
            "email": fields["email"],
            "phone": fields["phone"],
            "city": fields["city"],
            "street": fields["street"],
            "experience": fields["experience"],
            "start_date": fields["start_date"],
            "citizenship": 1 if fields["citizenship"] else 0,
            "english": 1 if fields["english"] else 0,
            "lang": fields["lang"],
            "created_at": now,
        },
    )
    await session.commit()
    return {"id": new_id, "created_at": now}


async def list_applications(
    session: AsyncSession, *, status_filter: str | None = None
) -> list[dict]:
    rows = (
        await session.execute(_LIST_APPLICATIONS, {"status_filter": status_filter})
    ).mappings().all()
    return [_application_row(r) for r in rows]


async def get_application(session: AsyncSession, *, application_id: int) -> dict | None:
    row = (
        await session.execute(_GET_APPLICATION, {"id": application_id})
    ).mappings().first()
    return _application_row(row) if row is not None else None


async def set_application_status(
    session: AsyncSession, *, application_id: int, status: str
) -> bool:
    if await get_application(session, application_id=application_id) is None:
        return False
    await session.execute(
        _SET_APPLICATION_STATUS, {"id": application_id, "status": status}
    )
    await session.commit()
    return True


async def set_application_cv_key(
    session: AsyncSession, *, application_id: int, cv_key: str
) -> None:
    await session.execute(
        _SET_APPLICATION_CV, {"id": application_id, "cv_key": cv_key}
    )
    await session.commit()


async def delete_application(session: AsyncSession, *, application_id: int) -> bool:
    if await get_application(session, application_id=application_id) is None:
        return False
    await session.execute(_DELETE_APPLICATION, {"id": application_id})
    await session.commit()
    return True


__all__ = [
    "list_positions",
    "get_position",
    "create_position",
    "update_position",
    "set_position_active",
    "delete_position",
    "create_application",
    "list_applications",
    "get_application",
    "set_application_status",
    "set_application_cv_key",
    "delete_application",
]
