"""Staff clock-in/out (QR session) router — thin, identity-seam gated endpoints.

Reimplements the live Firestore ``clock_sessions`` realtime handoff as a portable
Postgres short-TTL row plus a poll endpoint (no Firestore, no realtime listeners).

Two access tiers, both token-verified:

* **station (operator)** — DB-RBAC via :func:`be.app.deps.require_admin` on the
  ``station`` page (``full_admin`` bypasses). Mints a session (returns the QR
  payload), polls its status, lists active sessions, and can force-close one.
* **staff-self** — :class:`PrincipalDep` verifies the token; the redeeming
  *employee* is resolved from the VERIFIED identity (email/phone claims) via
  ``hr.crud.find_employee_by_identity`` — NEVER a body/query field. The written
  ``shifts`` row is for that resolved employee, so a member clocks only THEMSELVES.

Redeem enforces: single-use (atomic pending->used claim), TTL (lazy expiry on
read), and venue binding (the employee's venue must match the session's).

DEFERRED (noted, not built): realtime push (the station polls instead of a
Firestore ``onSnapshot`` listener); physical kiosk-device specifics; and Telegram
clock notifications (wire later behind a Notifier adapter).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from be.adapters.types import Principal
from be.app.deps import (
    AdminContext,
    DbDep,
    PrincipalDep,
    VenueDep,
    require_admin,
)
from be.app.domains.clock import crud, service
from be.app.domains.clock.schemas import RedeemSession
from be.app.domains.hr import crud as hr_crud
from be.app.domains.hr import service as hr_service

router = APIRouter(tags=["clock"])

StationAdmin = Annotated[AdminContext, Depends(require_admin("station"))]


# --- staff identity resolution (same seam as the scheduling router) ---------


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


def _display_name(emp: dict) -> str | None:
    name = f"{emp.get('name') or ''} {emp.get('surname') or ''}".strip()
    return name or None


# ══════════════════════════════════════════════════════════════════
# STATION (operator) — require_admin("station")
# ══════════════════════════════════════════════════════════════════


@router.post("/clock/sessions")
async def create_clock_session(
    _admin: StationAdmin, venue_id: VenueDep, session: DbDep
) -> dict:
    """Mint a pending clock session for the station's venue. The FE renders a QR from
    ``qr_payload`` and polls ``GET /clock/sessions/{token}`` for the outcome."""
    return await crud.create_session(session, venue_id=venue_id)


@router.get("/clock/sessions")
async def list_clock_sessions(
    _admin: StationAdmin, venue_id: VenueDep, session: DbDep
) -> list[dict]:
    """Active (pending/used) sessions for the venue; a past-TTL pending row is flagged
    ``expired`` in the view (without a write — the lazy DB flip happens on poll)."""
    rows = await crud.list_active_sessions(session, venue_id=venue_id)
    out: list[dict] = []
    for r in rows:
        expired = r["status"] == service.STATUS_PENDING and service.is_expired(
            r["expires_at"]
        )
        out.append(
            {
                "id": r["id"],
                "token": r["token"],
                "status": service.STATUS_EXPIRED if expired else r["status"],
                "kind": r["kind"],
                "employee_name": r["employee_name"],
                "created_at": r["created_at"],
                "expires_at": r["expires_at"],
            }
        )
    return out


@router.get("/clock/sessions/{token}")
async def poll_clock_session(
    token: str, _admin: StationAdmin, session: DbDep
) -> dict:
    """Poll a session's status (replaces the Firestore ``onSnapshot`` listener).

    A ``pending`` row past its TTL is lazily flipped to ``expired`` here.
    """
    sess = await crud.get_session_by_token(session, token=token)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    status = sess["status"]
    if status == service.STATUS_PENDING and service.is_expired(sess["expires_at"]):
        await crud.mark_expired(session, session_id=sess["id"])
        status = service.STATUS_EXPIRED
    return {
        "status": status,
        "kind": sess["kind"],
        "employee_name": sess["employee_name"],
        "expires_at": sess["expires_at"],
    }


@router.post("/clock/sessions/{session_id}/close")
async def force_close_clock_session(
    session_id: int, _admin: StationAdmin, session: DbDep
) -> dict:
    """Force-close a session (idempotent: closing a completed/expired one is a no-op)."""
    sess = await crud.get_session_by_id(session, session_id=session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    await crud.mark_expired(session, session_id=session_id)
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# STAFF-SELF — require_principal (employee from the VERIFIED token)
# ══════════════════════════════════════════════════════════════════


@router.post("/clock/redeem")
async def redeem_clock_session(
    payload: RedeemSession, principal: PrincipalDep, session: DbDep
) -> dict:
    """Redeem a scanned session -> clock the calling employee in or out.

    Auto-detects direction: an open shift today -> clock_out (stamp end_time), else
    clock_in (open a new ``source='clock'`` shift). Enforces single-use, TTL and
    venue binding. The employee is the token-resolved one, so a body can't name
    someone else.
    """
    token = payload.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="token is required")

    sess = await crud.get_session_by_token(session, token=token)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")

    # TTL: lazily expire a stale pending row, then reject anything not pending.
    if sess["status"] == service.STATUS_PENDING and service.is_expired(
        sess["expires_at"]
    ):
        await crud.mark_expired(session, session_id=sess["id"])
        raise HTTPException(status_code=410, detail="session expired")
    if sess["status"] == service.STATUS_EXPIRED:
        raise HTTPException(status_code=410, detail="session expired")
    if sess["status"] != service.STATUS_PENDING:
        raise HTTPException(status_code=409, detail="session already used")

    emp = await _resolve_employee(principal, session)

    # Venue binding: the employee's venue must match the session's venue.
    emp_venue = await crud.get_employee_venue(session, employee_id=emp["id"])
    if emp_venue != sess["venue_id"]:
        raise HTTPException(status_code=403, detail="session belongs to another venue")

    # Single-use: only one caller wins the atomic pending -> used claim.
    if not await crud.claim_session(session, session_id=sess["id"]):
        raise HTTPException(status_code=409, detail="session already used")

    now = service.now()
    shift_date = now.date().isoformat()
    hhmm = now.strftime("%H:%M")
    open_shift = await crud.get_open_shift_today(
        session, employee_id=emp["id"], shift_date=shift_date
    )
    if open_shift is not None:
        kind = service.CLOCK_OUT
        shift_id = int(open_shift["id"])
        await crud.clock_out(session, shift_id=shift_id, end_time=hhmm)
    else:
        kind = service.CLOCK_IN
        shift_id = await crud.clock_in(
            session,
            venue_id=sess["venue_id"],
            employee_id=emp["id"],
            shift_date=shift_date,
            start_time=hhmm,
        )

    name = _display_name(emp)
    await crud.mark_completed(
        session,
        session_id=sess["id"],
        employee_id=emp["id"],
        kind=kind,
        shift_id=shift_id,
        employee_name=name,
    )
    return {
        "status": service.STATUS_COMPLETED,
        "kind": kind,
        "shift_id": shift_id,
        "employee_name": name,
    }


__all__ = ["router"]
