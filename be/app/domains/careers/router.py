"""Careers router — public apply/listing + admin management, adapter-seamed.

Three access tiers:

* **public (NO auth)** — ``GET /careers/positions`` (active-only, display projection,
  ZERO applicant PII) and ``POST /careers/applications`` (write-only — echoes just the
  new id) plus ``POST /careers/applications/{id}/cv`` (one-shot CV attach via the
  Storage seam). These are the ONLY unauthenticated endpoints in core, so they are
  hardened deliberately: an in-process rate-limit guard (per client IP), strict input
  validation (``service.normalize_application``), and CV size/MIME caps. The public
  surface NEVER reads an application back.
* **admin (``require_admin("careers")``)** — position CRUD + toggle, application list/
  detail (full PII), status update, delete, and the CV download (proxy-streamed through
  the backend — Cloud Run can't sign; mirrors the forms-PDF / suggestions-image
  posture). CV download is admin-only with ``Content-Disposition: attachment``.

Adapter touch points: CV bytes via :class:`Storage`; the new-application notification via
the best-effort :class:`Notifier` seam.

DEFERRED (noted, not built here):
* the notification transport — routed through the Notifier seam (NoopNotifier by
  default); no email is sent from core.
* a distributed/production rate-limiter backend (the in-process guard does not share
  state across Cloud Run instances — a shared limiter is required for multi-instance
  prod).
"""

from __future__ import annotations

import mimetypes
import uuid
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import Response

from be.adapters.errors import AdapterError
from be.adapters.notify.base import Notifier
from be.adapters.storage.base import Storage
from be.app.deps import (
    AdminContext,
    DbDep,
    SettingsDep,
    get_notify,
    get_storage,
    require_admin,
)
from be.app.domains.careers import crud, service
from be.app.domains.careers.schemas import (
    ApplicationCreate,
    ApplicationStatusUpdate,
    PositionCreate,
    PositionUpdate,
)
from be.app.domains.careers.service import FixedWindowRateLimiter

router = APIRouter(tags=["careers"])

CareersAdmin = Annotated[AdminContext, Depends(require_admin("careers"))]
StorageDep = Annotated[Storage, Depends(get_storage)]
NotifierDep = Annotated[Notifier, Depends(get_notify)]

# In-process guard for the public endpoints (pluggable seam — see module docstring).
_rate_limiter = FixedWindowRateLimiter(max_requests=10, window_seconds=60.0)


def get_rate_limiter() -> FixedWindowRateLimiter:
    """Bind the shared in-process rate limiter (overridable in tests / prod swap)."""
    return _rate_limiter


RateLimiterDep = Annotated[FixedWindowRateLimiter, Depends(get_rate_limiter)]


# --- helpers ----------------------------------------------------------------


def _client_key(request: Request, scope: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"careers:{scope}:{host}"


def _enforce_rate_limit(limiter: FixedWindowRateLimiter, request: Request, scope: str) -> None:
    if not limiter.allow(_client_key(request, scope)):
        raise HTTPException(status_code=429, detail="too many requests, try again later")


def _public_position(p: dict) -> dict:
    """Display projection for the PUBLIC listing (positions carry no PII)."""
    return {
        "id": p["id"],
        "department": p["department"],
        "work_mode": p["work_mode"],
        "title_en": p["title_en"],
        "title_he": p["title_he"],
        "location_en": p["location_en"],
        "location_he": p["location_he"],
        "salary_en": p["salary_en"],
        "salary_he": p["salary_he"],
        "summary_en": p["summary_en"],
        "summary_he": p["summary_he"],
        "responsibilities_en": p["responsibilities_en"],
        "responsibilities_he": p["responsibilities_he"],
        "requirements_en": p["requirements_en"],
        "requirements_he": p["requirements_he"],
    }


# ══════════════════════════════════════════════════════════════════
# PUBLIC — open positions + apply (NO auth; rate-limited + validated)
# ══════════════════════════════════════════════════════════════════


@router.get("/careers/positions")
async def public_list_positions(session: DbDep) -> list[dict]:
    """List ACTIVE positions only, projected to display fields — no applicant PII."""
    rows = await crud.list_positions(session, active_only=True)
    return [_public_position(p) for p in rows]


@router.post("/careers/applications")
async def public_apply(
    payload: ApplicationCreate,
    request: Request,
    session: DbDep,
    settings: SettingsDep,
    notifier: NotifierDep,
    limiter: RateLimiterDep,
) -> dict:
    """Public, write-only apply. Validates server-side, echoes only the new id."""
    _enforce_rate_limit(limiter, request, "apply")
    try:
        fields = service.normalize_application(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Resolve the position (soft ref) and snapshot its titles; unknown -> null ref.
    position_title_en = ""
    position_title_he = ""
    if payload.position_id is not None:
        position = await crud.get_position(session, position_id=payload.position_id)
        if position is None or not position["is_active"]:
            raise HTTPException(status_code=404, detail="position not found")
        position_title_en = position["title_en"]
        position_title_he = position["title_he"]

    created = await crud.create_application(
        session,
        venue_id=settings.default_venue_id,
        position_id=payload.position_id,
        position_title_en=position_title_en,
        position_title_he=position_title_he,
        fields=fields,
    )

    # Best-effort new-application notification (Notifier seam; Noop by default — the
    # transport is DEFERRED). No PII beyond the applicant name/position is forwarded.
    await notifier.send(
        "careers_application_new",
        {
            "application_id": created["id"],
            "position_id": payload.position_id,
            "position_title_en": position_title_en,
            "applicant_name": fields["full_name"],
        },
    )
    return {"id": created["id"]}


@router.post("/careers/applications/{application_id}/cv")
async def public_upload_cv(
    application_id: int,
    request: Request,
    session: DbDep,
    storage: StorageDep,
    limiter: RateLimiterDep,
    cv: UploadFile = File(...),
) -> dict:
    """Attach a CV to a just-submitted application (one-shot; rate-limited + validated).

    One-shot: rejected with 409 if a CV is already attached — this keeps an anonymous
    caller from overwriting another applicant's CV by guessing the id.
    """
    _enforce_rate_limit(limiter, request, "cv")
    application = await crud.get_application(session, application_id=application_id)
    if application is None:
        raise HTTPException(status_code=404, detail="application not found")
    if application["cv_key"]:
        raise HTTPException(status_code=409, detail="a CV is already attached")

    content_type = (cv.content_type or "").lower()
    if content_type not in service.ALLOWED_CV_TYPES:
        raise HTTPException(status_code=400, detail="unsupported CV type (PDF or Word only)")
    data = await cv.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > service.MAX_CV_BYTES:
        raise HTTPException(status_code=413, detail="CV is too large (max 10MB)")

    key = (
        f"careers/applications/{application_id}/"
        f"{uuid.uuid4().hex}_{service.safe_filename(cv.filename)}"
    )
    stored = await storage.put(key, data, content_type)
    await crud.set_application_cv_key(session, application_id=application_id, cv_key=stored)
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# ADMIN — position management (require_admin("careers"))
# ══════════════════════════════════════════════════════════════════


@router.get("/admin/careers/positions")
async def admin_list_positions(_admin: CareersAdmin, session: DbDep) -> list[dict]:
    return await crud.list_positions(session, active_only=False)


@router.get("/admin/careers/positions/{position_id}")
async def admin_get_position(
    position_id: int, _admin: CareersAdmin, session: DbDep
) -> dict:
    position = await crud.get_position(session, position_id=position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="position not found")
    return position


@router.post("/admin/careers/positions")
async def admin_create_position(
    payload: PositionCreate,
    _admin: CareersAdmin,
    session: DbDep,
    settings: SettingsDep,
) -> dict:
    try:
        values = {
            "department": service.normalize_department(payload.department),
            "work_mode": service.normalize_work_mode(payload.work_mode),
            "title_en": payload.title_en.strip(),
            "title_he": payload.title_he.strip(),
            "location_en": payload.location_en.strip(),
            "location_he": payload.location_he.strip(),
            "salary_en": payload.salary_en.strip(),
            "salary_he": payload.salary_he.strip(),
            "summary_en": payload.summary_en.strip(),
            "summary_he": payload.summary_he.strip(),
            "responsibilities_en": service.normalize_string_list(payload.responsibilities_en),
            "responsibilities_he": service.normalize_string_list(payload.responsibilities_he),
            "requirements_en": service.normalize_string_list(payload.requirements_en),
            "requirements_he": service.normalize_string_list(payload.requirements_he),
            "sort_order": payload.sort_order,
            "is_active": payload.is_active,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await crud.create_position(session, venue_id=settings.default_venue_id, values=values)


@router.put("/admin/careers/positions/{position_id}")
async def admin_update_position(
    position_id: int,
    payload: PositionUpdate,
    _admin: CareersAdmin,
    session: DbDep,
) -> dict:
    provided = payload.model_dump(exclude_unset=True)
    changes: dict = {}
    try:
        for key, value in provided.items():
            if key == "department":
                changes[key] = service.normalize_department(value)
            elif key == "work_mode":
                changes[key] = service.normalize_work_mode(value)
            elif key in (
                "responsibilities_en",
                "responsibilities_he",
                "requirements_en",
                "requirements_he",
            ):
                changes[key] = service.normalize_string_list(value)
            elif key in ("sort_order", "is_active"):
                changes[key] = value
            else:  # text fields
                changes[key] = (value or "").strip()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    updated = await crud.update_position(session, position_id=position_id, changes=changes)
    if updated is None:
        raise HTTPException(status_code=404, detail="position not found")
    return updated


@router.post("/admin/careers/positions/{position_id}/active")
async def admin_toggle_position(
    position_id: int,
    payload: dict,
    _admin: CareersAdmin,
    session: DbDep,
) -> dict:
    active = bool(payload.get("is_active", True))
    updated = await crud.set_position_active(session, position_id=position_id, active=active)
    if updated is None:
        raise HTTPException(status_code=404, detail="position not found")
    return updated


@router.delete("/admin/careers/positions/{position_id}")
async def admin_delete_position(
    position_id: int, _admin: CareersAdmin, session: DbDep
) -> dict:
    if not await crud.delete_position(session, position_id=position_id):
        raise HTTPException(status_code=404, detail="position not found")
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# ADMIN — application review + CV download (require_admin("careers"))
# ══════════════════════════════════════════════════════════════════


@router.get("/admin/careers/applications")
async def admin_list_applications(
    _admin: CareersAdmin,
    session: DbDep,
    status: str | None = Query(None),
) -> list[dict]:
    if status is not None:
        try:
            status = service.validate_status(status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await crud.list_applications(session, status_filter=status)


@router.get("/admin/careers/applications/{application_id}")
async def admin_get_application(
    application_id: int, _admin: CareersAdmin, session: DbDep
) -> dict:
    application = await crud.get_application(session, application_id=application_id)
    if application is None:
        raise HTTPException(status_code=404, detail="application not found")
    return application


@router.put("/admin/careers/applications/{application_id}/status")
async def admin_update_application_status(
    application_id: int,
    payload: ApplicationStatusUpdate,
    _admin: CareersAdmin,
    session: DbDep,
) -> dict:
    try:
        status = service.validate_status(payload.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not await crud.set_application_status(
        session, application_id=application_id, status=status
    ):
        raise HTTPException(status_code=404, detail="application not found")
    return {"ok": True, "status": status}


@router.delete("/admin/careers/applications/{application_id}")
async def admin_delete_application(
    application_id: int, _admin: CareersAdmin, session: DbDep
) -> dict:
    if not await crud.delete_application(session, application_id=application_id):
        raise HTTPException(status_code=404, detail="application not found")
    return {"ok": True}


@router.get("/admin/careers/applications/{application_id}/cv")
async def admin_download_cv(
    application_id: int,
    _admin: CareersAdmin,
    session: DbDep,
    storage: StorageDep,
) -> Response:
    """Proxy-stream an application's CV (admin-only; the bucket blocks public access +
    signed-URL signing on Cloud Run — mirror the forms-PDF / suggestions-image posture)."""
    application = await crud.get_application(session, application_id=application_id)
    if application is None:
        raise HTTPException(status_code=404, detail="application not found")
    cv_key = application["cv_key"]
    if not cv_key:
        raise HTTPException(status_code=404, detail="no CV attached")
    try:
        data = await storage.get(cv_key)
    except (AdapterError, FileNotFoundError, KeyError) as exc:
        raise HTTPException(status_code=404, detail="CV not found") from exc
    media_type = mimetypes.guess_type(cv_key)[0] or "application/octet-stream"
    filename = cv_key.rsplit("/", 1)[-1]
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )


__all__ = ["router"]
