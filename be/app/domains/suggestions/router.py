"""Staff suggestions router — thin, identity-seam gated endpoints.

Two access tiers:

* **staff-self** — :class:`PrincipalDep` verifies the token; the calling *employee* is
  resolved from the VERIFIED identity (email/phone claims) via
  ``hr.crud.find_employee_by_identity`` — NEVER a query-param/body email. This closes
  the live impersonation hole in the v1 ``/staff/suggestions`` endpoints. Ownership is
  enforced here: a hidden suggestion is 404 unless the caller authored it, and only the
  author may attach images to their own suggestion / comment.
* **admin** — DB-RBAC via :func:`be.app.deps.require_admin` on the ``suggestions`` page.
  This FIXES the live bespoke ``_require_admin`` email-spoof hole (which trusted an
  ``email``/``phone`` **query param**): authority now comes from an active ``admin_users``
  row resolved from the verified token, not from a caller-asserted address.

Vendor touch points go through adapters: images via :class:`Storage`
(proxy-streamed back through ``/suggestions/image`` — Cloud Run can't sign), and the
new-suggestion notification via the best-effort :class:`Notifier` seam.

DEFERRED (noted, not built here): the actual notification transport — routed through
the Notifier seam (NoopNotifier by default), no email is sent from core; any bilingual
(he/en) subject/body specifics.
"""

from __future__ import annotations

import mimetypes
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response

from be.adapters.errors import AdapterError
from be.adapters.notify.base import Notifier
from be.adapters.storage.base import Storage
from be.adapters.types import Principal
from be.app.deps import (
    AdminContext,
    DbDep,
    PrincipalDep,
    VenueDep,
    get_notify,
    get_storage,
    require_admin,
)
from be.app.domains.hr import crud as hr_crud
from be.app.domains.hr import service as hr_service
from be.app.domains.suggestions import crud, service
from be.app.domains.suggestions.schemas import (
    AdminUpdate,
    CommentCreate,
    PlusOneResponse,
    SettingsUpdate,
    SuggestionCreate,
)

router = APIRouter(tags=["suggestions"])

SuggestionsAdmin = Annotated[AdminContext, Depends(require_admin("suggestions"))]
StorageDep = Annotated[Storage, Depends(get_storage)]
NotifierDep = Annotated[Notifier, Depends(get_notify)]


# --- helpers ----------------------------------------------------------------


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


async def _visible_head_or_404(
    session: DbDep, *, suggestion_id: int, viewer_id: int
) -> dict:
    """Load a suggestion head, enforcing hidden -> author-only visibility (else 404)."""
    head = await crud.get_head(session, suggestion_id=suggestion_id)
    if head is None or (head["is_hidden"] and head["employee_id"] != viewer_id):
        raise HTTPException(status_code=404, detail="suggestion not found")
    return head


async def _store_images(
    storage: Storage, files: list[UploadFile] | None, *, prefix: str
) -> list[str]:
    """Validate + persist image uploads via the Storage seam; return stored keys."""
    incoming = [f for f in (files or []) if f and f.filename]
    if not incoming:
        return []
    if len(incoming) > service.MAX_IMAGES:
        raise HTTPException(status_code=400, detail=f"too many images (max {service.MAX_IMAGES})")
    keys: list[str] = []
    for f in incoming:
        content_type = (f.content_type or "").lower()
        if not content_type.startswith(service.ALLOWED_IMAGE_PREFIX):
            raise HTTPException(status_code=400, detail=f"'{f.filename}' is not an image")
        data = await f.read()
        if not data:
            continue
        if len(data) > service.MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail=f"'{f.filename}' is too large (max 8MB)")
        key = f"{prefix}/{uuid.uuid4().hex}_{service.safe_filename(f.filename)}"
        keys.append(await storage.put(key, data, content_type))
    return keys


# ══════════════════════════════════════════════════════════════════
# STAFF-SELF — suggestions board (require_principal + ownership)
# ══════════════════════════════════════════════════════════════════


@router.get("/staff/suggestions")
async def staff_list_suggestions(
    principal: PrincipalDep,
    session: DbDep,
    status: str | None = Query(None),
) -> list[dict]:
    emp = await _resolve_employee(principal, session)
    rows = await crud.list_suggestions(
        session, viewer_id=emp["id"], include_hidden=False, status_filter=status
    )
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "category": r["category"],
            "description": r["description"],
            "status": r["status"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "author": r["author"],
            "is_own": r["employee_id"] == emp["id"],
            "plus_one_count": r["plus_one_count"],
            "viewer_plus_oned": r["viewer_plus_oned"],
            "comment_count": r["comment_count"],
            "thumbnail_url": r["thumbnail_url"],
        }
        for r in rows
    ]


@router.get("/staff/suggestions/{suggestion_id}")
async def staff_get_suggestion(
    suggestion_id: int, principal: PrincipalDep, session: DbDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    await _visible_head_or_404(session, suggestion_id=suggestion_id, viewer_id=emp["id"])
    detail = await crud.get_suggestion_detail(
        session, suggestion_id=suggestion_id, viewer_id=emp["id"]
    )
    assert detail is not None  # noqa: S101 - head existence checked above
    return {
        "id": detail["id"],
        "title": detail["title"],
        "category": detail["category"],
        "description": detail["description"],
        "status": detail["status"],
        "is_hidden": detail["is_hidden"],
        "created_at": detail["created_at"],
        "updated_at": detail["updated_at"],
        "author": detail["author"],
        "is_own": detail["employee_id"] == emp["id"],
        "plus_one_count": detail["plus_one_count"],
        "viewer_plus_oned": detail["viewer_plus_oned"],
        "images": [{"id": i["id"], "url": i["url"]} for i in detail["images"]],
        "comments": [
            {
                "id": c["id"],
                "body": c["body"],
                "created_at": c["created_at"],
                "author": c["author"],
                "is_own": c["employee_id"] == emp["id"],
                "images": c["images"],
            }
            for c in detail["comments"]
        ],
    }


@router.post("/staff/suggestions")
async def staff_create_suggestion(
    payload: SuggestionCreate,
    principal: PrincipalDep,
    session: DbDep,
    venue_id: VenueDep,
    notifier: NotifierDep,
) -> dict:
    emp = await _resolve_employee(principal, session)
    try:
        title = service.normalize_title(payload.title)
        category = service.normalize_category(payload.category)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    created = await crud.create_suggestion(
        session,
        venue_id=venue_id,
        employee_id=emp["id"],
        title=title,
        category=category,
        description=(payload.description or "").strip(),
    )
    # Best-effort new-suggestion notification (Notifier seam; Noop by default —
    # the transport is DEFERRED). Never breaks the create transaction.
    settings = await crud.get_settings(session)
    author = " ".join(p for p in (emp.get("name"), emp.get("surname")) if p).strip()
    await notifier.send(
        "suggestion_new",
        {
            "to_email": settings["notification_email"],
            "suggestion_id": created["id"],
            "title": title,
            "category": category,
            "description": payload.description or "",
            "author_name": author,
        },
    )
    return {"id": created["id"], "created_at": created["created_at"]}


@router.post("/staff/suggestions/{suggestion_id}/images")
async def staff_upload_suggestion_images(
    suggestion_id: int,
    principal: PrincipalDep,
    session: DbDep,
    storage: StorageDep,
    images: list[UploadFile] = File(...),
) -> dict:
    emp = await _resolve_employee(principal, session)
    head = await _visible_head_or_404(
        session, suggestion_id=suggestion_id, viewer_id=emp["id"]
    )
    if head["employee_id"] != emp["id"]:
        raise HTTPException(status_code=403, detail="only the author can add images")
    keys = await _store_images(storage, images, prefix=f"suggestions/{suggestion_id}")
    await crud.attach_suggestion_images(session, suggestion_id=suggestion_id, urls=keys)
    return {"images": keys}


@router.post("/staff/suggestions/{suggestion_id}/plus-one", response_model=PlusOneResponse)
async def staff_plus_one(
    suggestion_id: int, principal: PrincipalDep, session: DbDep
) -> PlusOneResponse:
    emp = await _resolve_employee(principal, session)
    await _visible_head_or_404(session, suggestion_id=suggestion_id, viewer_id=emp["id"])
    await crud.add_plus_one(session, suggestion_id=suggestion_id, employee_id=emp["id"])
    count = await crud.count_plus_ones(session, suggestion_id=suggestion_id)
    return PlusOneResponse(plus_one_count=count, viewer_plus_oned=True)


@router.delete("/staff/suggestions/{suggestion_id}/plus-one", response_model=PlusOneResponse)
async def staff_remove_plus_one(
    suggestion_id: int, principal: PrincipalDep, session: DbDep
) -> PlusOneResponse:
    emp = await _resolve_employee(principal, session)
    await _visible_head_or_404(session, suggestion_id=suggestion_id, viewer_id=emp["id"])
    await crud.remove_plus_one(session, suggestion_id=suggestion_id, employee_id=emp["id"])
    count = await crud.count_plus_ones(session, suggestion_id=suggestion_id)
    return PlusOneResponse(plus_one_count=count, viewer_plus_oned=False)


@router.post("/staff/suggestions/{suggestion_id}/comments")
async def staff_add_comment(
    suggestion_id: int,
    payload: CommentCreate,
    principal: PrincipalDep,
    session: DbDep,
) -> dict:
    emp = await _resolve_employee(principal, session)
    head = await _visible_head_or_404(
        session, suggestion_id=suggestion_id, viewer_id=emp["id"]
    )
    if head["status"] == "closed":
        raise HTTPException(status_code=400, detail="suggestion is closed")
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="comment body is required")
    comment = await crud.create_comment(
        session, suggestion_id=suggestion_id, employee_id=emp["id"], body=body
    )
    author = " ".join(p for p in (emp.get("name"), emp.get("surname")) if p).strip()
    return {
        "id": comment["id"],
        "created_at": comment["created_at"],
        "author": author,
        "body": body,
    }


@router.post("/staff/suggestions/{suggestion_id}/comments/{comment_id}/images")
async def staff_upload_comment_images(
    suggestion_id: int,
    comment_id: int,
    principal: PrincipalDep,
    session: DbDep,
    storage: StorageDep,
    images: list[UploadFile] = File(...),
) -> dict:
    emp = await _resolve_employee(principal, session)
    await _visible_head_or_404(session, suggestion_id=suggestion_id, viewer_id=emp["id"])
    comment = await crud.get_comment_head(session, comment_id=comment_id)
    if comment is None or comment["suggestion_id"] != suggestion_id:
        raise HTTPException(status_code=404, detail="comment not found")
    if comment["employee_id"] != emp["id"]:
        raise HTTPException(status_code=403, detail="only the author can add images")
    keys = await _store_images(
        storage, images, prefix=f"suggestions/{suggestion_id}/comments/{comment_id}"
    )
    await crud.attach_comment_images(session, comment_id=comment_id, urls=keys)
    return {"images": keys}


@router.get("/suggestions/image")
async def suggestion_image(
    _principal: PrincipalDep,
    storage: StorageDep,
    path: str = Query(..., description="stored image object path (suggestions/…)"),
) -> Response:
    """Proxy-stream a suggestion/comment image through the backend (the bucket blocks
    public access + signed-URL signing on Cloud Run — mirror the forms-PDF posture)."""
    object_path = path.split("?", 1)[0].lstrip("/")
    if not object_path.startswith("suggestions/"):
        raise HTTPException(status_code=400, detail="invalid media path")
    try:
        data = await storage.get(object_path)
    except (AdapterError, FileNotFoundError, KeyError) as exc:
        raise HTTPException(status_code=404, detail="image not found") from exc
    media_type = mimetypes.guess_type(object_path)[0] or "image/jpeg"
    return Response(
        content=data, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"}
    )


# ══════════════════════════════════════════════════════════════════
# ADMIN — moderation + settings (require_admin("suggestions"))
# ══════════════════════════════════════════════════════════════════


@router.get("/admin/suggestions")
async def admin_list_suggestions(
    _admin: SuggestionsAdmin,
    session: DbDep,
    status: str | None = Query(None),
) -> list[dict]:
    rows = await crud.list_suggestions(
        session, viewer_id=0, include_hidden=True, status_filter=status
    )
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "category": r["category"],
            "description": r["description"],
            "status": r["status"],
            "is_hidden": r["is_hidden"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "author": r["author"],
            "plus_one_count": r["plus_one_count"],
            "comment_count": r["comment_count"],
            "thumbnail_url": r["thumbnail_url"],
        }
        for r in rows
    ]


@router.get("/admin/suggestions/{suggestion_id}")
async def admin_get_suggestion(
    suggestion_id: int, _admin: SuggestionsAdmin, session: DbDep
) -> dict:
    detail = await crud.get_suggestion_detail(
        session, suggestion_id=suggestion_id, viewer_id=0
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="suggestion not found")
    return detail


@router.put("/admin/suggestions/{suggestion_id}")
async def admin_update_suggestion(
    suggestion_id: int,
    payload: AdminUpdate,
    admin: SuggestionsAdmin,
    session: DbDep,
) -> dict:
    if payload.is_hidden is None and payload.status is None:
        raise HTTPException(status_code=400, detail="nothing to update")
    if await crud.get_head(session, suggestion_id=suggestion_id) is None:
        raise HTTPException(status_code=404, detail="suggestion not found")
    if payload.is_hidden is not None:
        await crud.set_hidden(
            session,
            suggestion_id=suggestion_id,
            hidden=payload.is_hidden,
            actor_email=admin.email,
        )
    if payload.status is not None:
        try:
            status = service.validate_status(payload.status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await crud.set_status(
            session,
            suggestion_id=suggestion_id,
            status=status,
            actor_email=admin.email,
        )
    return {"ok": True}


@router.get("/admin/suggestions-settings")
async def admin_get_settings(_admin: SuggestionsAdmin, session: DbDep) -> dict:
    return await crud.get_settings(session)


@router.put("/admin/suggestions-settings")
async def admin_update_settings(
    payload: SettingsUpdate, _admin: SuggestionsAdmin, session: DbDep
) -> dict:
    try:
        email = service.validate_email(payload.notification_email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await crud.update_settings(session, email=email)


__all__ = ["router"]
