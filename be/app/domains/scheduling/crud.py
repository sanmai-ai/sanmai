"""Scheduling / timesheet / time-off CRUD — ``text()`` SQL constants + async funcs.

All SQL is dialect-agnostic (sqlite in tests, postgres in prod), mirroring
``be.app.domains.hr.crud``:

* unqualified table names; surrogate ids allocated app-side (``_next_id``);
* booleans stored as INTEGER 0/1; times as TEXT "HH:MM"; dates as TEXT ISO;
* timestamps written as ISO strings from Python (no ``now()``);
* upserts / membership replaces are select-then-insert/update (no ``ON CONFLICT``);
* id lists use an expanding IN bind (no postgres-only ``ANY(...)``);
* the "latest change request per shift" and the recurring-shift merge are computed
  in Python (no ``LATERAL`` / ``DISTINCT ON``).

Ownership is enforced by the router (it resolves the employee from the verified
token); the staff-scoped writes here additionally constrain by ``employee_id`` so a
mismatched row simply matches nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from be.app.domains.scheduling import service

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _b(value: Any) -> bool:
    return bool(value)


def _num_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _rowcount(result: Any) -> int:
    """Portable affected-row count (``CursorResult.rowcount`` via ``getattr`` so the
    typed ``Result`` return of ``AsyncSession.execute`` doesn't upset mypy)."""
    return int(getattr(result, "rowcount", 0) or 0)


async def _next_id(session: AsyncSession, table: str) -> int:
    stmt = text(f"SELECT COALESCE(MAX(id), 0) + 1 AS n FROM {table}")  # noqa: S608 - fixed literal
    row = (await session.execute(stmt)).mappings().first()
    return int(row["n"]) if row is not None else 1


# --------------------------------------------------------------------------- #
# Employee identity / admin identity
# --------------------------------------------------------------------------- #

_EMPLOYEE_IDENTITY = text(
    "SELECT id, is_stable_schedule, venue_id FROM employees WHERE id = :id"
)

_ADMIN_ID = text(
    """
    SELECT id FROM admin_users
    WHERE is_active = 1
      AND (
        (CAST(:uid AS TEXT) IS NOT NULL AND uid = CAST(:uid AS TEXT))
        OR (CAST(:email AS TEXT) IS NOT NULL AND email = CAST(:email AS TEXT))
      )
    ORDER BY id
    """
)


async def get_admin_id(
    session: AsyncSession, *, uid: str | None, email: str | None
) -> int | None:
    """Resolve the acting admin's ``admin_users.id`` (recorded as the approver)."""
    row = (await session.execute(_ADMIN_ID, {"uid": uid, "email": email})).mappings().first()
    return int(row["id"]) if row is not None else None


async def get_employee_stable(session: AsyncSession, *, employee_id: int) -> bool:
    row = (
        await session.execute(_EMPLOYEE_IDENTITY, {"id": employee_id})
    ).mappings().first()
    return bool(row["is_stable_schedule"]) if row is not None else False


# --------------------------------------------------------------------------- #
# Schedules (weekly containers)
# --------------------------------------------------------------------------- #

_LIST_WEEKS = text(
    """
    SELECT id, week_start FROM schedules
    WHERE venue_id = :venue_id AND week_start >= :f AND week_start <= :t
    ORDER BY week_start
    """
)

_GET_WEEK = text(
    "SELECT id, venue_id, week_start FROM schedules WHERE id = :id"
)

_WEEK_BY_START = text(
    "SELECT id FROM schedules WHERE venue_id = :venue_id AND week_start = :ws"
)

_INSERT_WEEK = text(
    "INSERT INTO schedules (id, venue_id, week_start, created_at) "
    "VALUES (:id, :venue_id, :ws, :created_at)"
)


async def list_weeks(
    session: AsyncSession, *, venue_id: str, from_date: str, to_date: str
) -> list[dict]:
    rows = (
        await session.execute(
            _LIST_WEEKS, {"venue_id": venue_id, "f": from_date, "t": to_date}
        )
    ).mappings().all()
    return [{"id": r["id"], "week_start": r["week_start"]} for r in rows]


async def _get_or_create_week(
    session: AsyncSession, *, venue_id: str, week_start: str
) -> int:
    row = (
        await session.execute(_WEEK_BY_START, {"venue_id": venue_id, "ws": week_start})
    ).mappings().first()
    if row is not None:
        return int(row["id"])
    new_id = await _next_id(session, "schedules")
    await session.execute(
        _INSERT_WEEK,
        {"id": new_id, "venue_id": venue_id, "ws": week_start, "created_at": _now_iso()},
    )
    return new_id


async def create_week(
    session: AsyncSession, *, venue_id: str, week_start: str
) -> dict:
    sched_id = await _get_or_create_week(session, venue_id=venue_id, week_start=week_start)
    await session.commit()
    return {"id": sched_id, "week_start": week_start}


async def get_week(session: AsyncSession, *, schedule_id: int) -> dict | None:
    row = (await session.execute(_GET_WEEK, {"id": schedule_id})).mappings().first()
    return dict(row) if row is not None else None


_PUBLISH_ALL = text(
    "UPDATE scheduled_shifts SET status = 'pending' "
    "WHERE schedule_id = :sid AND status = 'draft'"
)

_PUBLISH_IDS = text(
    "UPDATE scheduled_shifts SET status = 'pending' "
    "WHERE schedule_id = :sid AND status = 'draft' AND id IN :ids"
).bindparams(bindparam("ids", expanding=True))

_UNPUBLISH = text(
    "UPDATE scheduled_shifts SET status = 'draft' "
    "WHERE schedule_id = :sid AND status = 'pending'"
)


async def publish_week(
    session: AsyncSession, *, schedule_id: int, shift_ids: list[int] | None
) -> int:
    if shift_ids:
        result = await session.execute(
            _PUBLISH_IDS, {"sid": schedule_id, "ids": shift_ids}
        )
    else:
        result = await session.execute(_PUBLISH_ALL, {"sid": schedule_id})
    await session.commit()
    return _rowcount(result)


async def unpublish_week(session: AsyncSession, *, schedule_id: int) -> int:
    result = await session.execute(_UNPUBLISH, {"sid": schedule_id})
    await session.commit()
    return _rowcount(result)


# --------------------------------------------------------------------------- #
# Scheduled shifts
# --------------------------------------------------------------------------- #

_WEEK_SHIFTS = text(
    """
    SELECT ss.id, ss.employee_id, e.name AS emp_name, e.surname AS emp_surname,
           ss.role_id, jr.name AS role_name, jr.color AS role_color,
           ss.shift_date, ss.start_time, ss.end_time, ss.status, ss.notes,
           ss.edited_after_approval
    FROM scheduled_shifts ss
    JOIN employees e ON e.id = ss.employee_id
    JOIN job_roles jr ON jr.id = ss.role_id
    WHERE ss.schedule_id = :sid
    ORDER BY ss.shift_date, ss.start_time
    """
)

_INSERT_SHIFT = text(
    """
    INSERT INTO scheduled_shifts
        (id, venue_id, schedule_id, employee_id, role_id, shift_date, start_time,
         end_time, status, notes, edited_after_approval, created_at)
    VALUES
        (:id, :venue_id, :sid, :eid, :rid, :sd, :st, :et, :status, :notes, 0, :created_at)
    """
)

_GET_SHIFT = text(
    "SELECT id, venue_id, schedule_id, employee_id, role_id, shift_date, start_time, "
    "end_time, status, notes, edited_after_approval FROM scheduled_shifts WHERE id = :id"
)

_DELETE_SHIFT = text("DELETE FROM scheduled_shifts WHERE id = :id")


async def get_week_shifts(session: AsyncSession, *, schedule_id: int) -> list[dict]:
    rows = (await session.execute(_WEEK_SHIFTS, {"sid": schedule_id})).mappings().all()
    return [
        {
            "id": r["id"],
            "employee_id": r["employee_id"],
            "employee_name": f"{r['emp_name']} {r['emp_surname']}".strip(),
            "role_id": r["role_id"],
            "role_name": r["role_name"],
            "role_color": r["role_color"],
            "shift_date": r["shift_date"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "status": r["status"],
            "notes": r["notes"],
            "edited_after_approval": _b(r["edited_after_approval"]),
        }
        for r in rows
    ]


async def get_scheduled_shift(session: AsyncSession, *, shift_id: int) -> dict | None:
    row = (await session.execute(_GET_SHIFT, {"id": shift_id})).mappings().first()
    return dict(row) if row is not None else None


async def assign_shift(
    session: AsyncSession,
    *,
    venue_id: str,
    schedule_id: int,
    employee_id: int,
    role_id: int,
    shift_date: str,
    start_time: str,
    end_time: str,
    notes: str | None,
    status: str,
) -> dict:
    new_id = await _next_id(session, "scheduled_shifts")
    await session.execute(
        _INSERT_SHIFT,
        {
            "id": new_id,
            "venue_id": venue_id,
            "sid": schedule_id,
            "eid": employee_id,
            "rid": role_id,
            "sd": shift_date,
            "st": start_time,
            "et": end_time,
            "status": status,
            "notes": notes,
            "created_at": _now_iso(),
        },
    )
    await session.commit()
    return {"id": new_id}


async def update_shift(
    session: AsyncSession, *, shift_id: int, fields: dict[str, Any]
) -> dict | None:
    """Update a scheduled shift. If an admin materially changes (time/role of) an
    already-APPROVED shift without setting status explicitly, bounce it to 'pending'
    and flag ``edited_after_approval`` so the employee must re-approve.
    """
    cur = await get_scheduled_shift(session, shift_id=shift_id)
    if cur is None:
        return None

    role_id = fields["role_id"] if "role_id" in fields else cur["role_id"]
    start_time = fields["start_time"] if "start_time" in fields else cur["start_time"]
    end_time = fields["end_time"] if "end_time" in fields else cur["end_time"]
    notes = fields["notes"] if "notes" in fields else cur["notes"]
    status = fields["status"] if fields.get("status") is not None else cur["status"]

    material_change = (
        role_id != cur["role_id"]
        or start_time != cur["start_time"]
        or end_time != cur["end_time"]
    )
    edited = _b(cur["edited_after_approval"])
    if material_change and cur["status"] == "approved" and fields.get("status") is None:
        status = "pending"
        edited = True

    await session.execute(
        text(
            "UPDATE scheduled_shifts SET role_id = :rid, start_time = :st, "
            "end_time = :et, notes = :notes, status = :status, "
            "edited_after_approval = :edited WHERE id = :id"
        ),
        {
            "id": shift_id,
            "rid": role_id,
            "st": start_time,
            "et": end_time,
            "notes": notes,
            "status": status,
            "edited": 1 if edited else 0,
        },
    )
    await session.commit()
    return await get_scheduled_shift(session, shift_id=shift_id)


async def delete_shift(session: AsyncSession, *, shift_id: int) -> bool:
    if await get_scheduled_shift(session, shift_id=shift_id) is None:
        return False
    await session.execute(_DELETE_SHIFT, {"id": shift_id})
    await session.commit()
    return True


# --------------------------------------------------------------------------- #
# Staff shift decisions (ownership-constrained)
# --------------------------------------------------------------------------- #

_APPROVE_SHIFT = text(
    "UPDATE scheduled_shifts SET status = 'approved', edited_after_approval = 0 "
    "WHERE id = :id AND employee_id = :eid AND status = 'pending'"
)

_DECLINE_SHIFT = text(
    "UPDATE scheduled_shifts SET status = 'declined', edited_after_approval = 0 "
    "WHERE id = :id AND employee_id = :eid AND status = 'pending'"
)


async def set_shift_decision(
    session: AsyncSession, *, shift_id: int, employee_id: int, decision: str
) -> bool:
    """Approve/decline the caller's own pending shift. Returns False if it wasn't
    the caller's shift or wasn't pending (the router maps that to 400/404)."""
    stmt = _APPROVE_SHIFT if decision == "approved" else _DECLINE_SHIFT
    result = await session.execute(stmt, {"id": shift_id, "eid": employee_id})
    await session.commit()
    return _rowcount(result) > 0


_APPROVE_ALL = text(
    """
    UPDATE scheduled_shifts SET status = 'approved', edited_after_approval = 0
    WHERE employee_id = :eid AND shift_date >= :ws AND shift_date <= :we
      AND status = 'pending'
      AND NOT EXISTS (
        SELECT 1 FROM shift_change_requests scr
        WHERE scr.scheduled_shift_id = scheduled_shifts.id
          AND scr.request_type = 'schedule_change'
          AND scr.status = 'pending_admin'
      )
    """
)


async def approve_all_week(
    session: AsyncSession, *, employee_id: int, week_start: str, week_end: str
) -> int:
    result = await session.execute(
        _APPROVE_ALL, {"eid": employee_id, "ws": week_start, "we": week_end}
    )
    await session.commit()
    return _rowcount(result)


# --------------------------------------------------------------------------- #
# My-shifts (scheduled + recurring merge)
# --------------------------------------------------------------------------- #

_MY_SCHEDULED = text(
    """
    SELECT ss.id, ss.shift_date, ss.start_time, ss.end_time, ss.status, ss.notes,
           ss.edited_after_approval, jr.name AS role_name, jr.color AS role_color
    FROM scheduled_shifts ss
    JOIN job_roles jr ON jr.id = ss.role_id
    WHERE ss.employee_id = :eid AND ss.shift_date >= :f AND ss.shift_date <= :t
      AND ss.status IN ('pending', 'approved', 'declined', 'cancelled')
    ORDER BY ss.shift_date, ss.start_time
    """
)

_MY_CHANGE_REQUESTS = text(
    """
    SELECT scheduled_shift_id, recurring_shift_id, target_date, status,
           requested_start_time, requested_end_time, admin_response_note,
           created_at, id
    FROM shift_change_requests
    WHERE employee_id = :eid AND request_type = 'schedule_change'
    ORDER BY created_at, id
    """
)

_MY_RECURRING = text(
    """
    SELECT rs.id, rs.day_of_week, rs.start_time, rs.end_time, rs.start_date,
           jr.name AS role_name, jr.color AS role_color
    FROM recurring_shifts rs
    JOIN job_roles jr ON jr.id = rs.role_id
    WHERE rs.employee_id = :eid AND rs.is_active = 1
    """
)


async def my_shifts(
    session: AsyncSession,
    *,
    employee_id: int,
    is_stable: bool,
    from_date: str,
    to_date: str,
) -> list[dict]:
    """Employee schedule view: real shifts (drafts excluded) merged with synthesized
    recurring shifts for stable-schedule staff. 'cancelled' rows are tombstones that
    suppress the recurring chip for that date but are not returned themselves.
    """
    scheduled = (
        await session.execute(
            _MY_SCHEDULED, {"eid": employee_id, "f": from_date, "t": to_date}
        )
    ).mappings().all()

    # Latest schedule-change request per scheduled_shift_id and per
    # (recurring_shift_id, target_date) — computed in Python (no LATERAL/DISTINCT ON).
    crs = (
        await session.execute(_MY_CHANGE_REQUESTS, {"eid": employee_id})
    ).mappings().all()
    latest_by_shift: dict[int, dict] = {}
    latest_by_recurring: dict[tuple[int, str], dict] = {}
    pending_recurring: set[tuple[int, str]] = set()
    pending_shift: set[int] = set()
    for r in crs:  # ordered oldest->newest, so the last write wins as "latest"
        if r["scheduled_shift_id"] is not None:
            latest_by_shift[r["scheduled_shift_id"]] = dict(r)
            if r["status"] == "pending_admin":
                pending_shift.add(r["scheduled_shift_id"])
            elif r["scheduled_shift_id"] in pending_shift:
                pending_shift.discard(r["scheduled_shift_id"])
        elif r["recurring_shift_id"] is not None and r["target_date"] is not None:
            key = (r["recurring_shift_id"], r["target_date"])
            latest_by_recurring[key] = dict(r)
            if r["status"] == "pending_admin":
                pending_recurring.add(key)
            elif key in pending_recurring:
                pending_recurring.discard(key)

    def _rejected(latest: dict | None) -> dict | None:
        if latest is None or latest["status"] != "rejected":
            return None
        return {
            "requested_start": latest["requested_start_time"],
            "requested_end": latest["requested_end_time"],
            "note": latest["admin_response_note"],
        }

    out: list[dict] = []
    for r in scheduled:
        if r["status"] == "cancelled":
            continue
        latest = latest_by_shift.get(r["id"])
        out.append(
            {
                "id": r["id"],
                "shift_date": r["shift_date"],
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "status": r["status"],
                "notes": r["notes"],
                "role_name": r["role_name"],
                "role_color": r["role_color"],
                "change_pending": r["id"] in pending_shift,
                "edited_after_approval": _b(r["edited_after_approval"]),
                "rejected_change": _rejected(latest),
            }
        )

    if is_stable:
        recurring = (
            await session.execute(_MY_RECURRING, {"eid": employee_id})
        ).mappings().all()
        covered = {r["shift_date"] for r in scheduled}  # incl. cancelled tombstones
        start = service.parse_date(from_date)
        end = service.parse_date(to_date)
        cur = start
        while cur <= end:
            iso = cur.isoformat()
            dow = service.day_of_week(cur)
            if iso not in covered:
                for rs in recurring:
                    if rs["day_of_week"] != dow:
                        continue
                    if rs["start_date"] is not None and iso < rs["start_date"]:
                        continue
                    key = (rs["id"], iso)
                    out.append(
                        {
                            "id": None,
                            "recurring_shift_id": rs["id"],
                            "shift_date": iso,
                            "start_time": rs["start_time"],
                            "end_time": rs["end_time"],
                            "status": "stable",
                            "notes": None,
                            "role_name": rs["role_name"],
                            "role_color": rs["role_color"],
                            "change_pending": key in pending_recurring,
                            "edited_after_approval": False,
                            "rejected_change": _rejected(latest_by_recurring.get(key)),
                        }
                    )
            cur += timedelta(days=1)
        out.sort(key=lambda s: (s["shift_date"], s["start_time"]))

    return out


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #

_GET_AVAILABILITY = text(
    "SELECT day_of_week, is_available, note FROM employee_availability "
    "WHERE employee_id = :eid ORDER BY day_of_week"
)

_AVAILABILITY_ROW = text(
    "SELECT id FROM employee_availability WHERE employee_id = :eid AND day_of_week = :dow"
)

_INSERT_AVAILABILITY = text(
    "INSERT INTO employee_availability (id, employee_id, day_of_week, is_available, note) "
    "VALUES (:id, :eid, :dow, :avail, :note)"
)

_UPDATE_AVAILABILITY = text(
    "UPDATE employee_availability SET is_available = :avail, note = :note WHERE id = :id"
)


async def get_availability(session: AsyncSession, *, employee_id: int) -> list[dict]:
    rows = (
        await session.execute(_GET_AVAILABILITY, {"eid": employee_id})
    ).mappings().all()
    return [
        {
            "day_of_week": r["day_of_week"],
            "is_available": _b(r["is_available"]),
            "note": r["note"],
        }
        for r in rows
    ]


async def set_availability(
    session: AsyncSession, *, employee_id: int, items: list[dict]
) -> None:
    """Per-day upsert (select-then-insert/update; no ON CONFLICT)."""
    for item in items:
        existing = (
            await session.execute(
                _AVAILABILITY_ROW, {"eid": employee_id, "dow": item["day_of_week"]}
            )
        ).mappings().first()
        params = {
            "eid": employee_id,
            "dow": item["day_of_week"],
            "avail": 1 if item["is_available"] else 0,
            "note": item["note"],
        }
        if existing is None:
            new_id = await _next_id(session, "employee_availability")
            await session.execute(_INSERT_AVAILABILITY, {"id": new_id, **params})
        else:
            await session.execute(
                _UPDATE_AVAILABILITY,
                {"id": existing["id"], "avail": params["avail"], "note": params["note"]},
            )
    await session.commit()


# --------------------------------------------------------------------------- #
# Recurring shifts
# --------------------------------------------------------------------------- #

_LIST_RECURRING = text(
    """
    SELECT rs.id, rs.employee_id, e.name AS emp_name, e.surname AS emp_surname,
           rs.day_of_week, rs.role_id, jr.name AS role_name, jr.color AS role_color,
           rs.start_time, rs.end_time, rs.is_active, rs.start_date
    FROM recurring_shifts rs
    JOIN employees e ON e.id = rs.employee_id
    JOIN job_roles jr ON jr.id = rs.role_id
    ORDER BY e.name, rs.day_of_week
    """
)

_LIST_RECURRING_EMP = text(
    """
    SELECT rs.id, rs.employee_id, e.name AS emp_name, e.surname AS emp_surname,
           rs.day_of_week, rs.role_id, jr.name AS role_name, jr.color AS role_color,
           rs.start_time, rs.end_time, rs.is_active, rs.start_date
    FROM recurring_shifts rs
    JOIN employees e ON e.id = rs.employee_id
    JOIN job_roles jr ON jr.id = rs.role_id
    WHERE rs.employee_id = :eid
    ORDER BY e.name, rs.day_of_week
    """
)

_GET_RECURRING = text(
    "SELECT id, venue_id, employee_id, day_of_week, role_id, start_time, end_time, "
    "is_active, start_date FROM recurring_shifts WHERE id = :id"
)

_INSERT_RECURRING = text(
    """
    INSERT INTO recurring_shifts
        (id, venue_id, employee_id, day_of_week, role_id, start_time, end_time,
         is_active, start_date, created_at)
    VALUES
        (:id, :venue_id, :eid, :dow, :rid, :st, :et, 1, :sd, :created_at)
    """
)


def _row_to_recurring(r: Any) -> dict:
    return {
        "id": r["id"],
        "employee_id": r["employee_id"],
        "employee_name": f"{r['emp_name']} {r['emp_surname']}".strip(),
        "day_of_week": r["day_of_week"],
        "role_id": r["role_id"],
        "role_name": r["role_name"],
        "role_color": r["role_color"],
        "start_time": r["start_time"],
        "end_time": r["end_time"],
        "is_active": _b(r["is_active"]),
        "start_date": r["start_date"],
    }


async def list_recurring_shifts(
    session: AsyncSession, *, employee_id: int | None
) -> list[dict]:
    if employee_id is not None:
        rows = (
            await session.execute(_LIST_RECURRING_EMP, {"eid": employee_id})
        ).mappings().all()
    else:
        rows = (await session.execute(_LIST_RECURRING)).mappings().all()
    return [_row_to_recurring(r) for r in rows]


async def get_recurring_shift(session: AsyncSession, *, shift_id: int) -> dict | None:
    row = (await session.execute(_GET_RECURRING, {"id": shift_id})).mappings().first()
    return dict(row) if row is not None else None


async def create_recurring_shift(
    session: AsyncSession,
    *,
    venue_id: str,
    employee_id: int,
    day_of_week: int,
    role_id: int,
    start_time: str,
    end_time: str,
    start_date: str,
) -> dict:
    new_id = await _next_id(session, "recurring_shifts")
    await session.execute(
        _INSERT_RECURRING,
        {
            "id": new_id,
            "venue_id": venue_id,
            "eid": employee_id,
            "dow": day_of_week,
            "rid": role_id,
            "st": start_time,
            "et": end_time,
            "sd": start_date,
            "created_at": _now_iso(),
        },
    )
    await session.commit()
    return {"id": new_id}


async def update_recurring_shift(
    session: AsyncSession, *, shift_id: int, fields: dict[str, Any]
) -> dict | None:
    cur = await get_recurring_shift(session, shift_id=shift_id)
    if cur is None:
        return None
    role_id = fields["role_id"] if "role_id" in fields else cur["role_id"]
    start_time = fields["start_time"] if "start_time" in fields else cur["start_time"]
    end_time = fields["end_time"] if "end_time" in fields else cur["end_time"]
    is_active = (
        bool(fields["is_active"]) if fields.get("is_active") is not None else _b(cur["is_active"])
    )
    await session.execute(
        text(
            "UPDATE recurring_shifts SET role_id = :rid, start_time = :st, "
            "end_time = :et, is_active = :active WHERE id = :id"
        ),
        {
            "id": shift_id,
            "rid": role_id,
            "st": start_time,
            "et": end_time,
            "active": 1 if is_active else 0,
        },
    )
    await session.commit()
    return await get_recurring_shift(session, shift_id=shift_id)


async def delete_recurring_shift(session: AsyncSession, *, shift_id: int) -> bool:
    if await get_recurring_shift(session, shift_id=shift_id) is None:
        return False
    # Drop dependent change requests + sever clock-record links (portable equivalents
    # of the live cascade; clock rows keep their payroll snapshots).
    await session.execute(
        text("DELETE FROM shift_change_requests WHERE recurring_shift_id = :id"),
        {"id": shift_id},
    )
    await session.execute(
        text("UPDATE shifts SET recurring_shift_id = NULL WHERE recurring_shift_id = :id"),
        {"id": shift_id},
    )
    await session.execute(
        text("DELETE FROM recurring_shifts WHERE id = :id"), {"id": shift_id}
    )
    await session.commit()
    return True


# --------------------------------------------------------------------------- #
# Schedule-change requests
# --------------------------------------------------------------------------- #

_INSERT_SCHEDULE_CHANGE = text(
    """
    INSERT INTO shift_change_requests
        (id, venue_id, employee_id, request_type, scheduled_shift_id,
         recurring_shift_id, target_date, requested_start_time, requested_end_time,
         original_start_time, original_end_time, reason, status, created_at)
    VALUES
        (:id, :venue_id, :eid, 'schedule_change', :ssid, :rsid, :td, :st, :et,
         :ost, :oet, :reason, 'pending_admin', :created_at)
    """
)

_GET_CHANGE_REQUEST = text(
    "SELECT id, venue_id, employee_id, request_type, scheduled_shift_id, "
    "recurring_shift_id, target_date, shift_id, requested_start_time, "
    "requested_end_time, status FROM shift_change_requests WHERE id = :id"
)


async def create_schedule_change(
    session: AsyncSession,
    *,
    venue_id: str,
    employee_id: int,
    scheduled_shift_id: int,
    original_start: str,
    original_end: str,
    requested_start: str,
    requested_end: str,
    reason: str | None,
) -> int:
    new_id = await _next_id(session, "shift_change_requests")
    await session.execute(
        _INSERT_SCHEDULE_CHANGE,
        {
            "id": new_id,
            "venue_id": venue_id,
            "eid": employee_id,
            "ssid": scheduled_shift_id,
            "rsid": None,
            "td": None,
            "st": requested_start,
            "et": requested_end,
            "ost": original_start,
            "oet": original_end,
            "reason": reason,
            "created_at": _now_iso(),
        },
    )
    await session.commit()
    return new_id


async def create_recurring_change(
    session: AsyncSession,
    *,
    venue_id: str,
    employee_id: int,
    recurring_shift_id: int,
    target_date: str,
    original_start: str,
    original_end: str,
    requested_start: str,
    requested_end: str,
    reason: str | None,
) -> int:
    new_id = await _next_id(session, "shift_change_requests")
    await session.execute(
        _INSERT_SCHEDULE_CHANGE,
        {
            "id": new_id,
            "venue_id": venue_id,
            "eid": employee_id,
            "ssid": None,
            "rsid": recurring_shift_id,
            "td": target_date,
            "st": requested_start,
            "et": requested_end,
            "ost": original_start,
            "oet": original_end,
            "reason": reason,
            "created_at": _now_iso(),
        },
    )
    await session.commit()
    return new_id


_LIST_CHANGE_REQUESTS = text(
    """
    SELECT scr.id, scr.scheduled_shift_id, scr.recurring_shift_id, scr.target_date,
           scr.employee_id, e.name AS emp_name, e.surname AS emp_surname,
           scr.original_start_time, scr.original_end_time,
           scr.requested_start_time, scr.requested_end_time,
           scr.reason, scr.status, scr.created_at,
           ss.shift_date AS ss_date, jr1.name AS role1, jr2.name AS role2
    FROM shift_change_requests scr
    JOIN employees e ON e.id = scr.employee_id
    LEFT JOIN scheduled_shifts ss ON ss.id = scr.scheduled_shift_id
    LEFT JOIN recurring_shifts rs ON rs.id = scr.recurring_shift_id
    LEFT JOIN job_roles jr1 ON jr1.id = ss.role_id
    LEFT JOIN job_roles jr2 ON jr2.id = rs.role_id
    WHERE scr.request_type = 'schedule_change'
      AND (CAST(:status AS TEXT) IS NULL OR scr.status = CAST(:status AS TEXT))
    ORDER BY scr.created_at DESC, scr.id DESC
    """
)


async def list_change_requests(
    session: AsyncSession, *, status: str | None
) -> list[dict]:
    rows = (
        await session.execute(_LIST_CHANGE_REQUESTS, {"status": status})
    ).mappings().all()
    return [
        {
            "id": r["id"],
            "scheduled_shift_id": r["scheduled_shift_id"],
            "recurring_shift_id": r["recurring_shift_id"],
            "employee_id": r["employee_id"],
            "employee_name": f"{r['emp_name']} {r['emp_surname']}".strip(),
            "shift_date": r["ss_date"] or r["target_date"],
            "original_start": r["original_start_time"],
            "original_end": r["original_end_time"],
            "requested_start": r["requested_start_time"],
            "requested_end": r["requested_end_time"],
            "reason": r["reason"],
            "role_name": r["role1"] or r["role2"],
            "status": r["status"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


async def approve_schedule_change(
    session: AsyncSession, *, request_id: int, admin_id: int | None
) -> bool:
    """Approve a schedule-change request. For a scheduled shift: apply times. For a
    recurring shift: create an approved override scheduled_shift on the target date.
    Returns False if not found or not pending.
    """
    req = await get_change_request(session, request_id=request_id)
    if req is None or req["status"] != "pending_admin" or req["request_type"] != "schedule_change":
        return False

    if req["scheduled_shift_id"] is not None:
        await session.execute(
            text(
                "UPDATE scheduled_shifts SET start_time = :st, end_time = :et WHERE id = :id"
            ),
            {
                "st": req["requested_start_time"],
                "et": req["requested_end_time"],
                "id": req["scheduled_shift_id"],
            },
        )
    elif req["recurring_shift_id"] is not None and req["target_date"] is not None:
        rs = await get_recurring_shift(session, shift_id=req["recurring_shift_id"])
        if rs is not None:
            target = service.parse_date(req["target_date"])
            week_start = service.sunday_of_week(target).isoformat()
            sched_id = await _get_or_create_week(
                session, venue_id=rs["venue_id"], week_start=week_start
            )
            new_id = await _next_id(session, "scheduled_shifts")
            await session.execute(
                _INSERT_SHIFT,
                {
                    "id": new_id,
                    "venue_id": rs["venue_id"],
                    "sid": sched_id,
                    "eid": req["employee_id"],
                    "rid": rs["role_id"],
                    "sd": req["target_date"],
                    "st": req["requested_start_time"],
                    "et": req["requested_end_time"],
                    "status": "approved",
                    "notes": None,
                    "created_at": _now_iso(),
                },
            )

    await session.execute(
        text(
            "UPDATE shift_change_requests SET status = 'approved', "
            "admin_response_at = :ts, admin_response_by = :aid WHERE id = :id"
        ),
        {"ts": _now_iso(), "aid": admin_id, "id": request_id},
    )
    await session.commit()
    return True


async def reject_schedule_change(
    session: AsyncSession, *, request_id: int, admin_id: int | None, note: str | None
) -> bool:
    req = await get_change_request(session, request_id=request_id)
    if req is None or req["status"] != "pending_admin" or req["request_type"] != "schedule_change":
        return False
    await session.execute(
        text(
            "UPDATE shift_change_requests SET status = 'rejected', "
            "admin_response_at = :ts, admin_response_by = :aid, "
            "admin_response_note = :note WHERE id = :id"
        ),
        {"ts": _now_iso(), "aid": admin_id, "note": note, "id": request_id},
    )
    await session.commit()
    return True


async def get_change_request(
    session: AsyncSession, *, request_id: int
) -> dict | None:
    row = (
        await session.execute(_GET_CHANGE_REQUEST, {"id": request_id})
    ).mappings().first()
    return dict(row) if row is not None else None


# --------------------------------------------------------------------------- #
# Timesheet — worked shifts, confirmations, corrections
# --------------------------------------------------------------------------- #

_PENDING_CORRECTION = (
    "EXISTS (SELECT 1 FROM shift_change_requests scr WHERE scr.shift_id = s.id "
    "AND scr.request_type = 'timesheet_correction' AND scr.status = 'pending_admin')"
)

_MONTHLY_SHIFTS = text(
    f"""
    SELECT s.id, s.shift_date, s.start_time, s.end_time, s.confirmed,
           s.auto_close_ack, s.source, s.admin_comment,
           {_PENDING_CORRECTION} AS pending_correction
    FROM shifts s
    WHERE s.employee_id = :eid AND s.shift_date >= :f AND s.shift_date <= :t
    ORDER BY s.shift_date, s.start_time
    """  # noqa: S608 - _PENDING_CORRECTION is a fixed literal, no user input
)

_OWNED_SHIFT = text(
    f"""
    SELECT s.id, s.employee_id, s.shift_date, s.start_time, s.end_time, s.confirmed,
           s.auto_close_ack, s.source, {_PENDING_CORRECTION} AS pending_correction
    FROM shifts s WHERE s.id = :id AND s.employee_id = :eid
    """  # noqa: S608
)

_SHIFT_OWNER = text("SELECT id FROM shifts WHERE id = :id AND employee_id = :eid")
_SHIFT_LOCK = text("SELECT shift_date, confirmed FROM shifts WHERE id = :id")

_CONFIRM_SHIFT = text(
    "UPDATE shifts SET confirmed = 1 WHERE id = :id AND employee_id = :eid"
)

_CONFIRMABLE_IDS = text(
    f"""
    SELECT s.id FROM shifts s
    WHERE s.employee_id = :eid AND s.shift_date >= :f AND s.shift_date <= :t
      AND s.end_time IS NOT NULL AND s.confirmed = 0
      AND NOT (s.source = 'auto_closed' AND s.auto_close_ack = 0)
      AND NOT {_PENDING_CORRECTION}
    """  # noqa: S608
)

_CONFIRM_IDS = text(
    "UPDATE shifts SET confirmed = 1 WHERE id IN :ids"
).bindparams(bindparam("ids", expanding=True))

_INSERT_WORKED_SHIFT = text(
    """
    INSERT INTO shifts
        (id, venue_id, employee_id, role_id, shift_date, start_time, end_time,
         confirmed, auto_close_ack, source, created_at)
    VALUES
        (:id, :venue_id, :eid, :rid, :sd, :st, :et, 0, 0, :source, :created_at)
    """
)


async def get_monthly_shifts(
    session: AsyncSession, *, employee_id: int, first: str, last: str
) -> list[dict]:
    rows = (
        await session.execute(
            _MONTHLY_SHIFTS, {"eid": employee_id, "f": first, "t": last}
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def get_owned_shift(
    session: AsyncSession, *, shift_id: int, employee_id: int
) -> dict | None:
    row = (
        await session.execute(_OWNED_SHIFT, {"id": shift_id, "eid": employee_id})
    ).mappings().first()
    return dict(row) if row is not None else None


async def verify_shift_owner(
    session: AsyncSession, *, shift_id: int, employee_id: int
) -> bool:
    row = (
        await session.execute(_SHIFT_OWNER, {"id": shift_id, "eid": employee_id})
    ).first()
    return row is not None


async def get_shift_lock_info(session: AsyncSession, *, shift_id: int) -> dict | None:
    row = (await session.execute(_SHIFT_LOCK, {"id": shift_id})).mappings().first()
    if row is None:
        return None
    return {"shift_date": row["shift_date"], "confirmed": _b(row["confirmed"])}


async def confirm_shift(
    session: AsyncSession, *, shift_id: int, employee_id: int
) -> None:
    await session.execute(_CONFIRM_SHIFT, {"id": shift_id, "eid": employee_id})
    await session.commit()


async def confirm_month_shifts(
    session: AsyncSession, *, employee_id: int, first: str, last: str
) -> int:
    ids = [
        r["id"]
        for r in (
            await session.execute(
                _CONFIRMABLE_IDS, {"eid": employee_id, "f": first, "t": last}
            )
        ).mappings().all()
    ]
    if ids:
        await session.execute(_CONFIRM_IDS, {"ids": ids})
    return len(ids)


async def create_worked_shift(
    session: AsyncSession,
    *,
    venue_id: str,
    employee_id: int,
    shift_date: str,
    start_time: str,
    end_time: str,
    source: str = "forgot_clockin",
) -> int:
    new_id = await _next_id(session, "shifts")
    await session.execute(
        _INSERT_WORKED_SHIFT,
        {
            "id": new_id,
            "venue_id": venue_id,
            "eid": employee_id,
            "rid": None,
            "sd": shift_date,
            "st": start_time,
            "et": end_time,
            "source": source,
            "created_at": _now_iso(),
        },
    )
    return new_id


_GET_CONFIRMATION = text(
    "SELECT id, confirmed_at FROM timesheet_confirmations "
    "WHERE employee_id = :eid AND year = :y AND month = :m"
)

_INSERT_CONFIRMATION = text(
    "INSERT INTO timesheet_confirmations (id, employee_id, year, month, confirmed_at) "
    "VALUES (:id, :eid, :y, :m, :ts)"
)

_UPDATE_CONFIRMATION = text(
    "UPDATE timesheet_confirmations SET confirmed_at = :ts WHERE id = :id"
)


async def get_confirmation(
    session: AsyncSession, *, employee_id: int, year: int, month: int
) -> dict | None:
    row = (
        await session.execute(
            _GET_CONFIRMATION, {"eid": employee_id, "y": year, "m": month}
        )
    ).mappings().first()
    return dict(row) if row is not None else None


async def confirm_timesheet(
    session: AsyncSession, *, employee_id: int, year: int, month: int
) -> dict:
    """Audit-only month confirmation (select-then-insert/update; no ON CONFLICT)."""
    ts = _now_iso()
    existing = await get_confirmation(
        session, employee_id=employee_id, year=year, month=month
    )
    if existing is None:
        new_id = await _next_id(session, "timesheet_confirmations")
        await session.execute(
            _INSERT_CONFIRMATION,
            {"id": new_id, "eid": employee_id, "y": year, "m": month, "ts": ts},
        )
    else:
        await session.execute(_UPDATE_CONFIRMATION, {"ts": ts, "id": existing["id"]})
    # Commits both the audit row AND the confirm_month_shifts UPDATE queued earlier in
    # this session/transaction (that function intentionally defers the commit here).
    await session.commit()
    return {"confirmed_at": ts}


_MONTHLY_CORRECTIONS = text(
    """
    SELECT id, shift_id, requested_start_time, requested_end_time, correction_note,
           status, admin_response_note
    FROM shift_change_requests
    WHERE employee_id = :eid AND request_type = 'timesheet_correction'
      AND shift_id IN (
        SELECT id FROM shifts
        WHERE employee_id = :eid AND shift_date >= :f AND shift_date <= :t
      )
    ORDER BY created_at DESC, id DESC
    """
)

_PENDING_CORRECTION_ROW = text(
    "SELECT id FROM shift_change_requests WHERE shift_id = :sid "
    "AND request_type = 'timesheet_correction' AND status = 'pending_admin'"
)

_INSERT_CORRECTION = text(
    """
    INSERT INTO shift_change_requests
        (id, venue_id, employee_id, request_type, shift_id, requested_start_time,
         requested_end_time, correction_note, status, created_at)
    VALUES
        (:id, :venue_id, :eid, 'timesheet_correction', :shift_id, :st, :et, :note,
         'pending_admin', :created_at)
    """
)

_UPDATE_CORRECTION = text(
    "UPDATE shift_change_requests SET requested_start_time = :st, "
    "requested_end_time = :et, correction_note = :note, created_at = :created_at "
    "WHERE id = :id"
)

_UNCONFIRM_SHIFT = text("UPDATE shifts SET confirmed = 0 WHERE id = :id")


async def get_monthly_corrections(
    session: AsyncSession, *, employee_id: int, first: str, last: str
) -> list[dict]:
    rows = (
        await session.execute(
            _MONTHLY_CORRECTIONS, {"eid": employee_id, "f": first, "t": last}
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def create_correction(
    session: AsyncSession,
    *,
    venue_id: str,
    employee_id: int,
    shift_id: int,
    start_time: str | None,
    end_time: str | None,
    note: str | None,
) -> int:
    """Create/replace the single pending correction for a shift and un-confirm it
    (one-pending-correction-per-shift, select-then-update; no partial-unique race
    handling needed in the single-writer tests)."""
    existing = (
        await session.execute(_PENDING_CORRECTION_ROW, {"sid": shift_id})
    ).mappings().first()
    ts = _now_iso()
    if existing is not None:
        cid = int(existing["id"])
        await session.execute(
            _UPDATE_CORRECTION,
            {"id": cid, "st": start_time, "et": end_time, "note": note, "created_at": ts},
        )
    else:
        cid = await _next_id(session, "shift_change_requests")
        await session.execute(
            _INSERT_CORRECTION,
            {
                "id": cid,
                "venue_id": venue_id,
                "eid": employee_id,
                "shift_id": shift_id,
                "st": start_time,
                "et": end_time,
                "note": note,
                "created_at": ts,
            },
        )
    await session.execute(_UNCONFIRM_SHIFT, {"id": shift_id})
    await session.commit()
    return cid


# --------------------------------------------------------------------------- #
# Timesheet corrections — admin
# --------------------------------------------------------------------------- #

_LIST_CORRECTIONS = text(
    """
    SELECT scr.id, scr.shift_id, scr.employee_id,
           e.name AS emp_name, e.surname AS emp_surname,
           s.shift_date, s.start_time AS original_start, s.end_time AS original_end,
           scr.requested_start_time, scr.requested_end_time, scr.correction_note,
           scr.status, scr.created_at
    FROM shift_change_requests scr
    JOIN employees e ON e.id = scr.employee_id
    JOIN shifts s ON s.id = scr.shift_id
    WHERE scr.request_type = 'timesheet_correction'
      AND (CAST(:status AS TEXT) IS NULL OR scr.status = CAST(:status AS TEXT))
    ORDER BY scr.created_at DESC, scr.id DESC
    """
)

_GET_CORRECTION = text(
    "SELECT id, shift_id, requested_start_time, requested_end_time, status "
    "FROM shift_change_requests WHERE id = :id AND request_type = 'timesheet_correction'"
)


async def list_corrections(session: AsyncSession, *, status: str | None) -> list[dict]:
    rows = (
        await session.execute(_LIST_CORRECTIONS, {"status": status})
    ).mappings().all()
    return [
        {
            "id": r["id"],
            "shift_id": r["shift_id"],
            "employee_id": r["employee_id"],
            "employee_name": f"{r['emp_name']} {r['emp_surname']}".strip(),
            "shift_date": r["shift_date"],
            "original_start": r["original_start"],
            "original_end": r["original_end"],
            "requested_start_time": r["requested_start_time"],
            "requested_end_time": r["requested_end_time"],
            "correction_note": r["correction_note"],
            "status": r["status"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


async def approve_correction(
    session: AsyncSession, *, correction_id: int, admin_id: int | None
) -> bool:
    """Approve a timesheet correction: flip status, apply the requested shift times
    (COALESCE keeps an untouched side) and clear the unacked auto-close. False if
    not found / not pending."""
    row = (
        await session.execute(_GET_CORRECTION, {"id": correction_id})
    ).mappings().first()
    if row is None or row["status"] != "pending_admin":
        return False
    await session.execute(
        text(
            "UPDATE shift_change_requests SET status = 'approved', "
            "admin_response_at = :ts, admin_response_by = :aid WHERE id = :id"
        ),
        {"ts": _now_iso(), "aid": admin_id, "id": correction_id},
    )
    await session.execute(
        text(
            "UPDATE shifts SET "
            "start_time = COALESCE(:st, start_time), "
            "end_time = COALESCE(:et, end_time), "
            "auto_close_ack = 1 WHERE id = :id"
        ),
        {
            "st": row["requested_start_time"],
            "et": row["requested_end_time"],
            "id": row["shift_id"],
        },
    )
    await session.commit()
    return True


async def reject_correction(
    session: AsyncSession, *, correction_id: int, admin_id: int | None, note: str | None
) -> bool:
    row = (
        await session.execute(_GET_CORRECTION, {"id": correction_id})
    ).mappings().first()
    if row is None or row["status"] != "pending_admin":
        return False
    await session.execute(
        text(
            "UPDATE shift_change_requests SET status = 'rejected', "
            "admin_response_at = :ts, admin_response_by = :aid, "
            "admin_response_note = :note WHERE id = :id"
        ),
        {"ts": _now_iso(), "aid": admin_id, "note": note, "id": correction_id},
    )
    await session.commit()
    return True


# --------------------------------------------------------------------------- #
# Time off
# --------------------------------------------------------------------------- #

_OWN_TIME_OFF = text(
    """
    SELECT id, type, date_from, date_to, note, document_url, status,
           admin_response_note
    FROM time_off_requests
    WHERE employee_id = :eid AND date_from <= :t AND date_to >= :f
    ORDER BY date_from
    """
)

_TEAM_TIME_OFF = text(
    """
    SELECT t.employee_id, e.name AS emp_name, e.surname AS emp_surname,
           t.status, t.date_from, t.date_to, t.created_at
    FROM time_off_requests t
    JOIN employees e ON e.id = t.employee_id
    WHERE t.employee_id <> :eid AND t.status IN ('pending', 'approved')
      AND t.date_from <= :t AND t.date_to >= :f
    ORDER BY t.date_from
    """
)

_INSERT_TIME_OFF = text(
    """
    INSERT INTO time_off_requests
        (id, venue_id, employee_id, type, date_from, date_to, note, status, created_at)
    VALUES
        (:id, :venue_id, :eid, :type, :df, :dt, :note, 'pending', :created_at)
    """
)

_TIME_OFF_OWNER = text(
    "SELECT id, type, status FROM time_off_requests WHERE id = :id AND employee_id = :eid"
)

_UPDATE_TIME_OFF_DOC = text(
    "UPDATE time_off_requests SET document_url = :url WHERE id = :id"
)

_DELETE_TIME_OFF = text(
    "DELETE FROM time_off_requests WHERE id = :id AND employee_id = :eid AND status = 'pending'"
)

_GET_TIME_OFF = text(
    "SELECT id, employee_id, date_from, date_to, status FROM time_off_requests WHERE id = :id"
)


async def get_own_time_off(
    session: AsyncSession, *, employee_id: int, from_date: str, to_date: str
) -> list[dict]:
    rows = (
        await session.execute(
            _OWN_TIME_OFF, {"eid": employee_id, "f": from_date, "t": to_date}
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def get_team_time_off(
    session: AsyncSession, *, employee_id: int, from_date: str, to_date: str
) -> list[dict]:
    """Privacy-neutralized: never leak type/note/document_url/id of peers."""
    rows = (
        await session.execute(
            _TEAM_TIME_OFF, {"eid": employee_id, "f": from_date, "t": to_date}
        )
    ).mappings().all()
    return [
        {
            "employee_name": f"{r['emp_name']} {r['emp_surname']}".strip(),
            "kind": "off",
            "label": "Off",
            "status": r["status"],
            "date_from": r["date_from"],
            "date_to": r["date_to"],
            "requested_on": r["created_at"],
        }
        for r in rows
    ]


async def create_time_off(
    session: AsyncSession,
    *,
    venue_id: str,
    employee_id: int,
    type_: str,
    date_from: str,
    date_to: str,
    note: str | None,
) -> int:
    new_id = await _next_id(session, "time_off_requests")
    await session.execute(
        _INSERT_TIME_OFF,
        {
            "id": new_id,
            "venue_id": venue_id,
            "eid": employee_id,
            "type": type_,
            "df": date_from,
            "dt": date_to,
            "note": note,
            "created_at": _now_iso(),
        },
    )
    await session.commit()
    return new_id


async def verify_time_off_owner(
    session: AsyncSession, *, time_off_id: int, employee_id: int
) -> dict | None:
    row = (
        await session.execute(_TIME_OFF_OWNER, {"id": time_off_id, "eid": employee_id})
    ).mappings().first()
    return dict(row) if row is not None else None


async def update_time_off_doc(
    session: AsyncSession, *, time_off_id: int, url: str
) -> None:
    await session.execute(_UPDATE_TIME_OFF_DOC, {"id": time_off_id, "url": url})
    await session.commit()


async def delete_time_off(
    session: AsyncSession, *, time_off_id: int, employee_id: int
) -> bool:
    result = await session.execute(
        _DELETE_TIME_OFF, {"id": time_off_id, "eid": employee_id}
    )
    await session.commit()
    return _rowcount(result) > 0


_LIST_TIME_OFF = text(
    """
    SELECT t.id, t.employee_id, e.name AS emp_name, e.surname AS emp_surname,
           t.type, t.date_from, t.date_to, t.note, t.document_url, t.status, t.created_at
    FROM time_off_requests t
    JOIN employees e ON e.id = t.employee_id
    WHERE (CAST(:status AS TEXT) IS NULL OR t.status = CAST(:status AS TEXT))
      AND (CAST(:type AS TEXT) IS NULL OR t.type = CAST(:type AS TEXT))
      AND (CAST(:f AS TEXT) IS NULL OR t.date_to >= CAST(:f AS TEXT))
      AND (CAST(:t AS TEXT) IS NULL OR t.date_from <= CAST(:t AS TEXT))
    ORDER BY t.created_at DESC, t.id DESC
    """
)


async def list_time_off(
    session: AsyncSession,
    *,
    status: str | None,
    type_: str | None,
    from_date: str | None,
    to_date: str | None,
) -> list[dict]:
    rows = (
        await session.execute(
            _LIST_TIME_OFF,
            {"status": status, "type": type_, "f": from_date, "t": to_date},
        )
    ).mappings().all()
    return [
        {
            "id": r["id"],
            "employee_id": r["employee_id"],
            "employee_name": f"{r['emp_name']} {r['emp_surname']}".strip(),
            "type": r["type"],
            "date_from": r["date_from"],
            "date_to": r["date_to"],
            "note": r["note"],
            "document_url": r["document_url"],
            "status": r["status"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


async def respond_time_off(
    session: AsyncSession,
    *,
    time_off_id: int,
    status: str,
    admin_id: int | None,
    note: str | None,
) -> bool:
    """Approve/reject a pending time-off request. On approve, apply the schedule
    side-effect. Returns False if not found / already processed."""
    row = (
        await session.execute(_GET_TIME_OFF, {"id": time_off_id})
    ).mappings().first()
    if row is None or row["status"] != "pending":
        return False
    await session.execute(
        text(
            "UPDATE time_off_requests SET status = :status, admin_response_at = :ts, "
            "admin_response_by = :aid, admin_response_note = :note WHERE id = :id"
        ),
        {"status": status, "ts": _now_iso(), "aid": admin_id, "note": note, "id": time_off_id},
    )
    if status == "approved":
        await _apply_time_off_to_schedule(
            session,
            employee_id=row["employee_id"],
            date_from=row["date_from"],
            date_to=row["date_to"],
        )
    await session.commit()
    return True


async def admin_delete_time_off(session: AsyncSession, *, time_off_id: int) -> bool:
    row = (
        await session.execute(_GET_TIME_OFF, {"id": time_off_id})
    ).mappings().first()
    if row is None:
        return False
    await session.execute(
        text("DELETE FROM time_off_requests WHERE id = :id"), {"id": time_off_id}
    )
    await session.commit()
    return True


async def admin_create_time_off(
    session: AsyncSession,
    *,
    venue_id: str,
    employee_id: int,
    type_: str,
    date_from: str,
    date_to: str,
    note: str | None,
    admin_id: int | None,
) -> int:
    """Admin-created time-off lands approved and applies the schedule side-effect."""
    new_id = await _next_id(session, "time_off_requests")
    await session.execute(
        text(
            """
            INSERT INTO time_off_requests
                (id, venue_id, employee_id, type, date_from, date_to, note, status,
                 admin_response_at, admin_response_by, created_at)
            VALUES
                (:id, :venue_id, :eid, :type, :df, :dt, :note, 'approved', :ts, :aid, :ts)
            """
        ),
        {
            "id": new_id,
            "venue_id": venue_id,
            "eid": employee_id,
            "type": type_,
            "df": date_from,
            "dt": date_to,
            "note": note,
            "ts": _now_iso(),
            "aid": admin_id,
        },
    )
    await _apply_time_off_to_schedule(
        session, employee_id=employee_id, date_from=date_from, date_to=date_to
    )
    await session.commit()
    return new_id


_DELETE_EMP_SHIFTS_ON_DATE = text(
    "DELETE FROM scheduled_shifts WHERE employee_id = :eid AND shift_date = :d "
    "AND status IN ('draft', 'pending', 'approved', 'declined')"
)

_RECURRING_ON_DOW = text(
    "SELECT id, venue_id, role_id, start_time, end_time, start_date "
    "FROM recurring_shifts WHERE employee_id = :eid AND day_of_week = :dow AND is_active = 1"
)

_TOMBSTONE_EXISTS = text(
    "SELECT id FROM scheduled_shifts WHERE employee_id = :eid AND shift_date = :d "
    "AND role_id = :rid AND status = 'cancelled'"
)


async def _apply_time_off_to_schedule(
    session: AsyncSession, *, employee_id: int, date_from: str, date_to: str
) -> None:
    """For each date in [date_from, date_to]: delete the employee's real scheduled
    shifts and insert a 'cancelled' tombstone for each active recurring template on
    that weekday (portable rewrite: no ON CONFLICT/now()). No commit here — the
    caller owns the transaction."""
    cur = service.parse_date(date_from)
    end = service.parse_date(date_to)
    while cur <= end:
        iso = cur.isoformat()
        dow = service.day_of_week(cur)
        await session.execute(_DELETE_EMP_SHIFTS_ON_DATE, {"eid": employee_id, "d": iso})
        recurring = (
            await session.execute(_RECURRING_ON_DOW, {"eid": employee_id, "dow": dow})
        ).mappings().all()
        for rs in recurring:
            if rs["start_date"] is not None and iso < rs["start_date"]:
                continue
            exists = (
                await session.execute(
                    _TOMBSTONE_EXISTS, {"eid": employee_id, "d": iso, "rid": rs["role_id"]}
                )
            ).first()
            if exists is not None:
                continue
            new_id = await _next_id(session, "scheduled_shifts")
            week_start = service.sunday_of_week(cur).isoformat()
            sched_id = await _get_or_create_week(
                session, venue_id=rs["venue_id"], week_start=week_start
            )
            await session.execute(
                _INSERT_SHIFT,
                {
                    "id": new_id,
                    "venue_id": rs["venue_id"],
                    "sid": sched_id,
                    "eid": employee_id,
                    "rid": rs["role_id"],
                    "sd": iso,
                    "st": rs["start_time"],
                    "et": rs["end_time"],
                    "status": "cancelled",
                    "notes": None,
                    "created_at": _now_iso(),
                },
            )
        cur += timedelta(days=1)


# --------------------------------------------------------------------------- #
# Monthly working hours (config)
# --------------------------------------------------------------------------- #

_GET_WORKING_HOURS = text(
    "SELECT id, year, month, hours FROM monthly_working_hours "
    "WHERE venue_id = :venue_id AND year = :y AND month = :m"
)

_LIST_WORKING_HOURS = text(
    "SELECT year, month, hours FROM monthly_working_hours "
    "WHERE venue_id = :venue_id ORDER BY year, month"
)

_INSERT_WORKING_HOURS = text(
    "INSERT INTO monthly_working_hours (id, venue_id, year, month, hours) "
    "VALUES (:id, :venue_id, :y, :m, CAST(:hours AS numeric))"
)

_UPDATE_WORKING_HOURS = text(
    "UPDATE monthly_working_hours SET hours = CAST(:hours AS numeric) WHERE id = :id"
)


async def get_working_hours(
    session: AsyncSession, *, venue_id: str, year: int, month: int
) -> dict | None:
    row = (
        await session.execute(
            _GET_WORKING_HOURS, {"venue_id": venue_id, "y": year, "m": month}
        )
    ).mappings().first()
    if row is None:
        return None
    return {"year": row["year"], "month": row["month"], "hours": float(row["hours"])}


async def list_working_hours(session: AsyncSession, *, venue_id: str) -> list[dict]:
    rows = (
        await session.execute(_LIST_WORKING_HOURS, {"venue_id": venue_id})
    ).mappings().all()
    return [
        {"year": r["year"], "month": r["month"], "hours": float(r["hours"])} for r in rows
    ]


async def upsert_working_hours(
    session: AsyncSession, *, venue_id: str, year: int, month: int, hours: float
) -> dict:
    existing = (
        await session.execute(
            _GET_WORKING_HOURS, {"venue_id": venue_id, "y": year, "m": month}
        )
    ).mappings().first()
    if existing is None:
        new_id = await _next_id(session, "monthly_working_hours")
        await session.execute(
            _INSERT_WORKING_HOURS,
            {"id": new_id, "venue_id": venue_id, "y": year, "m": month, "hours": _num_str(hours)},
        )
    else:
        await session.execute(
            _UPDATE_WORKING_HOURS, {"id": existing["id"], "hours": _num_str(hours)}
        )
    await session.commit()
    result = await get_working_hours(session, venue_id=venue_id, year=year, month=month)
    assert result is not None
    return result
