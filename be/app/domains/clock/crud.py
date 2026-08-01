"""Clock domain CRUD — ``text()`` SQL constants + async funcs.

Portable (sqlite in tests, postgres in prod), mirroring ``scheduling.crud``:

* unqualified table names; surrogate ids allocated app-side (``_next_id``);
* timestamps written as ISO strings from Python (no ``now()``);
* the single-use claim is an atomic ``UPDATE ... WHERE status='pending'`` whose
  rowcount decides the winner (no ``ON CONFLICT``, no ``SELECT ... FOR UPDATE``).

The resulting ``shifts`` row keeps the exact shape defined by 0005_scheduling.sql
(``source='clock'``), so the timesheet endpoints read clock-written shifts with no
special-casing. Ownership/venue binding is enforced by the router (it resolves the
employee from the verified token); the writes here are constrained by ``employee_id``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from be.app.domains.clock import service

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return service.now().isoformat()


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


async def _next_id(session: AsyncSession, table: str) -> int:
    stmt = text(f"SELECT COALESCE(MAX(id), 0) + 1 AS n FROM {table}")  # noqa: S608 - fixed literal
    row = (await session.execute(stmt)).mappings().first()
    return int(row["n"]) if row is not None else 1


# --------------------------------------------------------------------------- #
# Clock sessions
# --------------------------------------------------------------------------- #

_INSERT_SESSION = text(
    """
    INSERT INTO clock_sessions
        (id, token, venue_id, status, employee_id, kind, shift_id, employee_name,
         created_at, expires_at, redeemed_at)
    VALUES
        (:id, :token, :venue_id, 'pending', NULL, NULL, NULL, NULL,
         :created_at, :expires_at, NULL)
    """
)

_GET_BY_TOKEN = text(
    "SELECT id, token, venue_id, status, employee_id, kind, shift_id, employee_name, "
    "created_at, expires_at, redeemed_at FROM clock_sessions WHERE token = :token"
)

_GET_BY_ID = text(
    "SELECT id, token, venue_id, status, employee_id, kind, shift_id, employee_name, "
    "created_at, expires_at, redeemed_at FROM clock_sessions WHERE id = :id"
)

_LIST_ACTIVE = text(
    """
    SELECT id, token, venue_id, status, employee_id, kind, shift_id, employee_name,
           created_at, expires_at
    FROM clock_sessions
    WHERE venue_id = :venue_id AND status IN ('pending', 'used')
    ORDER BY created_at DESC, id DESC
    """
)

# Atomic single-use claim: only a still-'pending' row transitions to 'used'.
_CLAIM_SESSION = text(
    "UPDATE clock_sessions SET status = 'used' WHERE id = :id AND status = 'pending'"
)

_MARK_EXPIRED = text(
    "UPDATE clock_sessions SET status = 'expired' WHERE id = :id "
    "AND status IN ('pending', 'used')"
)

_MARK_COMPLETED = text(
    """
    UPDATE clock_sessions
    SET status = 'completed', employee_id = :eid, kind = :kind, shift_id = :sid,
        employee_name = :name, redeemed_at = :ts
    WHERE id = :id
    """
)


async def create_session(
    session: AsyncSession, *, venue_id: str, at: datetime | None = None
) -> dict:
    """Mint a fresh pending session with a Python-computed TTL. Returns the QR bits."""
    created = at or service.now()
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    expires = service.expiry_for(created)
    token = service.new_token()
    new_id = await _next_id(session, "clock_sessions")
    await session.execute(
        _INSERT_SESSION,
        {
            "id": new_id,
            "token": token,
            "venue_id": venue_id,
            "created_at": created.isoformat(),
            "expires_at": expires.isoformat(),
        },
    )
    await session.commit()
    return {
        "id": new_id,
        "token": token,
        "qr_payload": service.qr_payload(token),
        "expires_at": expires.isoformat(),
    }


async def get_session_by_token(
    session: AsyncSession, *, token: str
) -> dict | None:
    row = (await session.execute(_GET_BY_TOKEN, {"token": token})).mappings().first()
    return dict(row) if row is not None else None


async def get_session_by_id(session: AsyncSession, *, session_id: int) -> dict | None:
    row = (await session.execute(_GET_BY_ID, {"id": session_id})).mappings().first()
    return dict(row) if row is not None else None


async def list_active_sessions(
    session: AsyncSession, *, venue_id: str
) -> list[dict]:
    rows = (await session.execute(_LIST_ACTIVE, {"venue_id": venue_id})).mappings().all()
    return [dict(r) for r in rows]


async def mark_expired(session: AsyncSession, *, session_id: int) -> bool:
    """Flip a pending/used session to expired (lazy TTL flip or admin force-close)."""
    result = await session.execute(_MARK_EXPIRED, {"id": session_id})
    await session.commit()
    return _rowcount(result) > 0


async def claim_session(session: AsyncSession, *, session_id: int) -> bool:
    """Atomically transition pending -> used. Returns False if already claimed/closed."""
    result = await session.execute(_CLAIM_SESSION, {"id": session_id})
    return _rowcount(result) > 0


async def mark_completed(
    session: AsyncSession,
    *,
    session_id: int,
    employee_id: int,
    kind: str,
    shift_id: int,
    employee_name: str | None,
) -> None:
    await session.execute(
        _MARK_COMPLETED,
        {
            "id": session_id,
            "eid": employee_id,
            "kind": kind,
            "sid": shift_id,
            "name": employee_name,
            "ts": _now_iso(),
        },
    )
    await session.commit()


# --------------------------------------------------------------------------- #
# Employee venue + shift writes (shifts shape per 0005_scheduling.sql)
# --------------------------------------------------------------------------- #

_EMPLOYEE_VENUE = text("SELECT venue_id FROM employees WHERE id = :id")

_OPEN_SHIFT_TODAY = text(
    "SELECT id, start_time FROM shifts "
    "WHERE employee_id = :eid AND shift_date = :sd AND end_time IS NULL "
    "ORDER BY id DESC"
)

_INSERT_CLOCK_SHIFT = text(
    """
    INSERT INTO shifts
        (id, venue_id, employee_id, role_id, shift_date, start_time, end_time,
         confirmed, auto_close_ack, source, created_at)
    VALUES
        (:id, :venue_id, :eid, NULL, :sd, :st, NULL, 0, 0, 'clock', :created_at)
    """
)

_CLOSE_SHIFT = text("UPDATE shifts SET end_time = :et WHERE id = :id")


async def get_employee_venue(
    session: AsyncSession, *, employee_id: int
) -> str | None:
    row = (
        await session.execute(_EMPLOYEE_VENUE, {"id": employee_id})
    ).mappings().first()
    return str(row["venue_id"]) if row is not None else None


async def get_open_shift_today(
    session: AsyncSession, *, employee_id: int, shift_date: str
) -> dict | None:
    row = (
        await session.execute(
            _OPEN_SHIFT_TODAY, {"eid": employee_id, "sd": shift_date}
        )
    ).mappings().first()
    return dict(row) if row is not None else None


async def clock_in(
    session: AsyncSession,
    *,
    venue_id: str,
    employee_id: int,
    shift_date: str,
    start_time: str,
) -> int:
    """Open a new worked shift (``source='clock'``, ``end_time`` NULL). No commit —
    the caller (redeem) commits atomically via :func:`mark_completed`."""
    new_id = await _next_id(session, "shifts")
    await session.execute(
        _INSERT_CLOCK_SHIFT,
        {
            "id": new_id,
            "venue_id": venue_id,
            "eid": employee_id,
            "sd": shift_date,
            "st": start_time,
            "created_at": _now_iso(),
        },
    )
    return new_id


async def clock_out(
    session: AsyncSession, *, shift_id: int, end_time: str
) -> None:
    """Close the open shift by stamping ``end_time``. No commit (see :func:`clock_in`)."""
    await session.execute(_CLOSE_SHIFT, {"id": shift_id, "et": end_time})


__all__ = [
    "create_session",
    "get_session_by_token",
    "get_session_by_id",
    "list_active_sessions",
    "mark_expired",
    "claim_session",
    "mark_completed",
    "get_employee_venue",
    "get_open_shift_today",
    "clock_in",
    "clock_out",
]
