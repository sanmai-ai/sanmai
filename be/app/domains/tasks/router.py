"""Tasks & checklists router — thin, identity-seam gated endpoints.

Two access tiers:

* **admin/manager** — DB-RBAC via :func:`be.app.deps.require_admin` on the ``tasks``
  page. ``full_admin`` bypasses the page gate and additionally owns the approval flow
  (approve/reject) and bonus authoring; every other holder of the ``tasks`` page is a
  *manager* whose checklist edits land in ``pending_approval`` (R36).
* **staff-self** — :class:`PrincipalDep` verifies the token; the calling *employee* is
  resolved from the VERIFIED identity (email/phone claims) via
  ``hr.crud.find_employee_by_identity`` — NEVER a query-param/body email. This closes
  the live impersonation holes in ``/staff/checklist-runs`` (query-param identity) and
  the v1 ``/staff/tasks`` endpoints. Ownership is then enforced by the crud guards: a
  caller may only read/act on a run they are a resolved assignee of, and only on tasks
  visible to them.

DEFERRED (noted, not built): the token-guarded ``/internal/generate-checklist-runs``
cron endpoint (the ``generate_pass`` logic lives in crud, wire a scheduler later);
Telegram/notify approval cards (wire behind a Notifier adapter); bonus crediting
(payroll domain — see the hook in ``crud.finish_task``); the v1 ``task_instances``
endpoints (superseded by the checklist-run flow — table kept for history only).
"""

from __future__ import annotations

import mimetypes
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response

from be.adapters.errors import AdapterError
from be.adapters.llm.base import LLMProvider
from be.adapters.storage.base import Storage
from be.adapters.types import Principal
from be.app.deps import (
    AdminContext,
    DbDep,
    PrincipalDep,
    VenueDep,
    get_llm,
    get_storage,
    require_admin,
)
from be.app.domains.hr import crud as hr_crud
from be.app.domains.hr import service as hr_service
from be.app.domains.tasks import crud, service
from be.app.domains.tasks.schemas import (
    AiDraftPayload,
    AssigneesSet,
    ChecklistCreate,
    ChecklistItemIn,
    ChecklistItemsSet,
    ChecklistUpdate,
    DependencyAdd,
    FinishPayload,
    GeneratePayload,
    QuickAssignPayload,
    RejectPayload,
    ReleasePayload,
    ReopenPayload,
    SubtaskPayload,
    TakeOverPayload,
    TemplateCreate,
    TemplateUpdate,
)

router = APIRouter(tags=["tasks"])

TasksAdmin = Annotated[AdminContext, Depends(require_admin("tasks"))]
StorageDep = Annotated[Storage, Depends(get_storage)]
LLMDep = Annotated[LLMProvider, Depends(get_llm)]

MAX_UPLOAD_SIZE = 8 * 1024 * 1024  # 8 MB / proof image


# --- helpers ----------------------------------------------------------------


def _is_full_admin(admin: AdminContext) -> bool:
    return admin.role == "full_admin"


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


def _parse_date_or_today(raw: str | None) -> str:
    if not raw:
        return date.today().isoformat()
    try:
        return service.parse_date(raw).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _norm_time_or_400(raw: str | None) -> str | None:
    try:
        return service.parse_hhmm(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ══════════════════════════════════════════════════════════════════
# ADMIN — TASK TEMPLATES (require_admin("tasks"))
# ══════════════════════════════════════════════════════════════════


@router.get("/tasks/templates")
async def list_templates(
    _admin: TasksAdmin,
    session: DbDep,
    q: str | None = Query(None),
    category: str | None = Query(None),
) -> list[dict]:
    return await crud.list_templates(session, q=q, category=category)


@router.post("/tasks/templates")
async def create_template(
    payload: TemplateCreate, admin: TasksAdmin, session: DbDep
) -> dict:
    try:
        service.validate_category(payload.category)
        service.validate_proof_type(payload.default_proof_type)
        service.validate_required_staff(payload.required_staff)
        subtasks = service.clean_subtasks(payload.subtasks)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if (payload.is_bonus or payload.bonus_amount is not None) and not _is_full_admin(admin):
        raise HTTPException(status_code=403, detail="only admins can create bonus tasks")
    fields = payload.model_dump()
    fields["subtasks"] = subtasks
    return await crud.create_template(session, fields=fields)


@router.put("/tasks/templates/{template_id}")
async def update_template(
    template_id: int, payload: TemplateUpdate, admin: TasksAdmin, session: DbDep
) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")
    try:
        service.validate_category(fields.get("category"))
        service.validate_proof_type(fields.get("default_proof_type"))
        service.validate_required_staff(fields.get("required_staff"))
        if "subtasks" in fields:
            fields["subtasks"] = service.clean_subtasks(fields["subtasks"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if ("is_bonus" in fields or "bonus_amount" in fields) and not _is_full_admin(admin):
        raise HTTPException(status_code=403, detail="only admins can edit bonus settings on tasks")
    if not await crud.update_template(session, template_id=template_id, fields=fields):
        raise HTTPException(status_code=404, detail="template not found")
    return {"ok": True}


@router.post("/tasks/templates/{template_id}/photos")
async def upload_template_photos(
    template_id: int,
    _admin: TasksAdmin,
    session: DbDep,
    storage: StorageDep,
    images: list[UploadFile] = File(...),
) -> dict:
    tpl = await crud.get_template(session, template_id=template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="template not found")
    existing = service.json_load(tpl["photos"], [])
    incoming = [f for f in (images or []) if f and f.filename]
    if len(existing) + len(incoming) > service.MAX_TEMPLATE_PHOTOS:
        raise HTTPException(
            status_code=400, detail=f"too many photos (max {service.MAX_TEMPLATE_PHOTOS} per task)"
        )
    urls = list(existing)
    for f in incoming:
        data = await f.read()
        if len(data) > MAX_UPLOAD_SIZE:
            raise HTTPException(status_code=413, detail="file too large (max 8MB)")
        key = f"tasks/templates/{template_id}/{f.filename}"
        urls.append(await storage.put(key, data, f.content_type or "application/octet-stream"))
    await crud.set_template_photos(session, template_id=template_id, photos=urls)
    return {"photos": urls}


@router.delete("/tasks/templates/{template_id}/photos")
async def delete_template_photo(
    template_id: int, _admin: TasksAdmin, session: DbDep, url: str = Query(...)
) -> dict:
    tpl = await crud.get_template(session, template_id=template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="template not found")
    photos = [p for p in service.json_load(tpl["photos"], []) if p != url]
    await crud.set_template_photos(session, template_id=template_id, photos=photos)
    return {"photos": photos}


@router.post("/tasks/templates/{template_id}/dependencies")
async def add_dependency(
    template_id: int, payload: DependencyAdd, _admin: TasksAdmin, session: DbDep
) -> dict:
    if template_id == payload.depends_on_id:
        raise HTTPException(status_code=400, detail="cannot depend on itself")
    await crud.add_dependency(session, template_id=template_id, depends_on_id=payload.depends_on_id)
    return {"ok": True}


@router.delete("/tasks/templates/{template_id}/dependencies/{dep_id}")
async def remove_dependency(
    template_id: int, dep_id: int, _admin: TasksAdmin, session: DbDep
) -> dict:
    await crud.remove_dependency(session, template_id=template_id, depends_on_id=dep_id)
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# ADMIN — CHECKLISTS (require_admin("tasks"))
# ══════════════════════════════════════════════════════════════════


@router.get("/tasks/checklists")
async def list_checklists(
    _admin: TasksAdmin, session: DbDep, approval_status: str | None = Query(None)
) -> list[dict]:
    return await crud.list_checklists(session, approval_status=approval_status)


@router.post("/tasks/checklists")
async def create_checklist(
    payload: ChecklistCreate, admin: TasksAdmin, session: DbDep
) -> dict:
    try:
        service.validate_category(payload.category)
        service.validate_recurrence(payload.recurrence)
        due = _norm_time_or_400(payload.due_time)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    approval = "approved" if _is_full_admin(admin) else "pending_approval"  # R36
    fields = payload.model_dump()
    fields["due_time"] = due
    return await crud.create_checklist(
        session, fields=fields, created_by=admin.email, approval_status=approval
    )


@router.get("/tasks/checklists/{checklist_id}")
async def get_checklist(checklist_id: int, _admin: TasksAdmin, session: DbDep) -> dict:
    cl = await crud.get_checklist(session, checklist_id=checklist_id)
    if cl is None:
        raise HTTPException(status_code=404, detail="checklist not found")
    return cl


@router.put("/tasks/checklists/{checklist_id}")
async def update_checklist(
    checklist_id: int, payload: ChecklistUpdate, admin: TasksAdmin, session: DbDep
) -> dict:
    await crud.stale_check(session, checklist_id=checklist_id, client_updated_at=payload.updated_at)
    fields = payload.model_dump(exclude_unset=True)
    fields.pop("updated_at", None)
    try:
        service.validate_category(fields.get("category"))
        service.validate_recurrence(fields.get("recurrence"))
        if "due_time" in fields:
            fields["due_time"] = _norm_time_or_400(fields["due_time"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    touched = await crud.update_checklist(
        session, checklist_id=checklist_id, fields=fields, is_full_admin=_is_full_admin(admin)
    )
    return {"ok": True, **touched}


@router.get("/tasks/checklists/{checklist_id}/items")
async def get_checklist_items(
    checklist_id: int, _admin: TasksAdmin, session: DbDep
) -> list[dict]:
    return await crud.get_checklist_items(session, checklist_id=checklist_id)


@router.put("/tasks/checklists/{checklist_id}/items")
async def set_checklist_items(
    checklist_id: int, payload: ChecklistItemsSet, admin: TasksAdmin, session: DbDep
) -> dict:
    await crud.stale_check(session, checklist_id=checklist_id, client_updated_at=payload.updated_at)
    if payload.items is not None:
        items = payload.items
    elif payload.template_ids is not None:
        items = [ChecklistItemIn(task_template_id=tid) for tid in payload.template_ids]
    else:
        raise HTTPException(status_code=400, detail="items or template_ids required")

    tids = [it.task_template_id for it in items]
    if len(tids) != len(set(tids)):
        raise HTTPException(status_code=400, detail="duplicate task in checklist")
    for it in items:
        try:
            service.validate_proof_type(it.proof_type)
            service.validate_required_staff(it.required_staff)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    touched = await crud.set_checklist_items(
        session,
        checklist_id=checklist_id,
        items=[it.model_dump() for it in items],
        is_full_admin=_is_full_admin(admin),
    )
    return {"ok": True, **touched}


# ── approval flow (R36) — full_admin only ─────────────────────────


@router.post("/tasks/checklists/{checklist_id}/approve")
async def approve_checklist(checklist_id: int, admin: TasksAdmin, session: DbDep) -> dict:
    if not _is_full_admin(admin):
        raise HTTPException(status_code=403, detail="only admins can approve checklists")
    if not await crud.approve_checklist(session, checklist_id=checklist_id, by=admin.email):
        raise HTTPException(status_code=404, detail="checklist not found")
    return {"ok": True, "approval_status": "approved"}


@router.post("/tasks/checklists/{checklist_id}/reject")
async def reject_checklist(
    checklist_id: int, payload: RejectPayload, admin: TasksAdmin, session: DbDep
) -> dict:
    if not _is_full_admin(admin):
        raise HTTPException(status_code=403, detail="only admins can reject checklists")
    if not await crud.reject_checklist(
        session, checklist_id=checklist_id, note=payload.note, by=admin.email
    ):
        raise HTTPException(status_code=404, detail="checklist not found")
    return {"ok": True, "approval_status": "rejected"}


# ══════════════════════════════════════════════════════════════════
# ADMIN — ASSIGNEES + resolution preview (require_admin("tasks"))
# ══════════════════════════════════════════════════════════════════


@router.get("/tasks/checklists/{checklist_id}/assignees")
async def get_assignees(checklist_id: int, _admin: TasksAdmin, session: DbDep) -> list[dict]:
    return await crud.get_assignees(session, checklist_id=checklist_id)


@router.put("/tasks/checklists/{checklist_id}/assignees")
async def set_assignees(
    checklist_id: int, payload: AssigneesSet, admin: TasksAdmin, session: DbDep, venue_id: VenueDep
) -> dict:
    await crud.stale_check(session, checklist_id=checklist_id, client_updated_at=payload.updated_at)
    normalized = []
    for a in payload.assignees:
        if a.schedule_scope not in service.VALID_SCHEDULE_SCOPES:
            raise HTTPException(status_code=400, detail="schedule_scope must be 'anyone' or 'scheduled'")
        subjects = sum(x is not None for x in (a.employee_id, a.group_id, a.role_id))
        if subjects > 1:
            raise HTTPException(status_code=400, detail="an assignee targets at most one of employee / group / role")
        if subjects == 0 and a.schedule_scope != "scheduled":
            raise HTTPException(status_code=400, detail="non-scheduled assignee needs an employee, group, or role")
        try:
            ws = _norm_time_or_400(a.window_start)
            we = _norm_time_or_400(a.window_end)
        except HTTPException:
            raise
        d = a.model_dump()
        d["window_start"], d["window_end"] = ws, we
        normalized.append(d)

    touched = await crud.set_assignees(
        session, checklist_id=checklist_id, assignees=normalized, is_full_admin=_is_full_admin(admin)
    )
    # Propagate new targets to already-generated open runs so people added after
    # publishing appear without a manual re-resolve.
    await crud.re_resolve_open_runs(session, checklist_id=checklist_id, today=date.today().isoformat())
    await session.commit()
    return {"ok": True, **touched}


@router.get("/tasks/checklists/{checklist_id}/resolve-assignees")
async def resolve_assignees(
    checklist_id: int,
    _admin: TasksAdmin,
    session: DbDep,
    run_date: str | None = Query(None, alias="date"),
) -> dict:
    d = _parse_date_or_today(run_date)
    names = await crud.resolve_preview(session, checklist_id=checklist_id, d=d)
    return {"date": d, "employees": names, "count": len(names)}


# ══════════════════════════════════════════════════════════════════
# ADMIN — QUICK ASSIGN + RUN GENERATION (require_admin("tasks"))
# ══════════════════════════════════════════════════════════════════


@router.post("/tasks/quick-assign")
async def quick_assign(
    payload: QuickAssignPayload, admin: TasksAdmin, session: DbDep, venue_id: VenueDep
) -> dict:
    if (payload.task_template_id is None) == (payload.new_task is None):
        raise HTTPException(status_code=400, detail="provide exactly one of task_template_id or new_task")
    if not payload.employee_ids:
        raise HTTPException(status_code=400, detail="employee_ids must contain at least one employee")

    d = _parse_date_or_today(payload.date)
    due = _norm_time_or_400(payload.due_time) if payload.due_time else None

    if payload.new_task is not None:
        nt = payload.new_task
        if not nt.title.strip():
            raise HTTPException(status_code=400, detail="new_task.title is required")
        try:
            service.validate_proof_type(nt.default_proof_type)
            service.validate_required_staff(nt.required_staff)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if (nt.is_bonus or nt.bonus_amount is not None) and not _is_full_admin(admin):
            raise HTTPException(status_code=403, detail="only admins can create bonus tasks")
        created = await crud.create_template(
            session,
            fields={
                "title": nt.title.strip(),
                "description": nt.description,
                "category": "during_shift",
                "estimated_minutes": nt.estimated_minutes,
                "is_bonus": nt.is_bonus,
                "bonus_amount": nt.bonus_amount,
                "default_proof_type": nt.default_proof_type,
                "required_staff": nt.required_staff,
            },
        )
        template_id = created["id"]
    else:
        assert payload.task_template_id is not None  # noqa: S101 - XOR guarded above
        template_id = payload.task_template_id
        tpl = await crud.get_template(session, template_id=template_id)
        if tpl is None:
            raise HTTPException(status_code=404, detail="task not found")
        if tpl["is_bonus"] and not _is_full_admin(admin):
            raise HTTPException(status_code=403, detail="only admins can quick-assign bonus tasks")

    result = await crud.quick_assign(
        session,
        task_template_id=template_id,
        employee_ids=payload.employee_ids,
        d=d,
        due_time=due,
        is_mandatory=payload.is_mandatory,
        actor=admin.email,
        venue_id=venue_id,
    )
    await session.commit()
    return result


@router.post("/tasks/checklists/{checklist_id}/runs")
async def generate_one_run(
    checklist_id: int, payload: GeneratePayload, admin: TasksAdmin, session: DbDep, venue_id: VenueDep
) -> dict:
    d = _parse_date_or_today(payload.date)
    cl = await crud.get_checklist_lean(session, checklist_id=checklist_id)
    if cl is None:
        raise HTTPException(status_code=404, detail="checklist not found")
    if not cl["is_active"] or cl["approval_status"] != "approved":
        raise HTTPException(status_code=400, detail="only approved, active checklists can generate runs")
    run_id = await crud.generate_run(
        session, checklist=dict(cl), d=d, generated_by=f"admin:{admin.email}", venue_id=venue_id
    )
    await session.commit()
    if run_id is None:
        return {"ok": False, "message": f"a run for {d} already exists", "run_id": None}
    return {"ok": True, "run_id": run_id, "date": d}


# ══════════════════════════════════════════════════════════════════
# ADMIN — RUNS BOARD + run actions (require_admin("tasks"))
# ══════════════════════════════════════════════════════════════════


@router.get("/tasks/runs")
async def admin_list_runs(
    _admin: TasksAdmin,
    session: DbDep,
    venue_id: VenueDep,
    run_date: str | None = Query(None, alias="date"),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    checklist_id: int | None = Query(None),
    employee_id: int | None = Query(None),
    status: str | None = Query(None),
) -> dict:
    df: str | None
    dt: str | None
    if run_date:
        df = dt = _parse_date_or_today(run_date)
    else:
        df = _parse_date_or_today(date_from) if date_from else None
        dt = _parse_date_or_today(date_to) if date_to else None
    rows = await crud.list_runs(
        session,
        venue_id=venue_id,
        date_from=df,
        date_to=dt,
        checklist_id=checklist_id,
        employee_id=employee_id,
        status=status,
    )
    return {"runs": rows}


@router.get("/tasks/runs/{run_id}")
async def admin_run_detail(run_id: int, _admin: TasksAdmin, session: DbDep) -> dict:
    return await crud.get_run_detail(session, run_id=run_id)


@router.post("/tasks/runs/{run_id}/re-resolve")
async def admin_re_resolve(run_id: int, _admin: TasksAdmin, session: DbDep) -> dict:
    result = await crud.re_resolve_run(session, run_id=run_id)
    await session.commit()
    return {"ok": True, **result}


@router.post("/tasks/runs/{run_id}/regenerate")
async def admin_regenerate(run_id: int, admin: TasksAdmin, session: DbDep) -> dict:
    new_id = await crud.regenerate_run(session, run_id=run_id, generated_by=f"admin:{admin.email}")
    return {"ok": True, "run_id": new_id}


@router.post("/tasks/runs/tasks/{run_task_id}/release")
async def admin_release_task(
    run_task_id: int, payload: ReleasePayload, admin: TasksAdmin, session: DbDep
) -> dict:
    released = await crud.admin_release(
        session, run_task_id=run_task_id, employee_id=payload.employee_id, actor=admin.email
    )
    return {"ok": True, "released": released}


@router.post("/tasks/runs/tasks/{run_task_id}/reopen")
async def admin_reopen_task(
    run_task_id: int, payload: ReopenPayload, admin: TasksAdmin, session: DbDep
) -> dict:
    reopened = await crud.admin_reopen(
        session, run_task_id=run_task_id, employee_id=payload.employee_id, actor=admin.email
    )
    return {"ok": True, "reopened": reopened}


@router.post("/tasks/runs/tasks/{run_task_id}/retro-complete")
async def admin_retro_complete(run_task_id: int, admin: TasksAdmin, session: DbDep) -> dict:
    await crud.retro_complete(session, run_task_id=run_task_id, actor=admin.email)
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# ADMIN — AI CHECKLIST DRAFTER + proof-image proxy (require_admin("tasks"))
# ══════════════════════════════════════════════════════════════════


@router.post("/admin/tasks/ai/draft-checklist")
async def ai_draft_checklist(
    payload: AiDraftPayload, _admin: TasksAdmin, session: DbDep, llm: LLMDep
) -> dict:
    """Draft a checklist from a prompt using the active task library as context.

    The model never writes to the DB (draft-and-review). Never proposes bonus/money.
    Degrades to an empty draft on any failure — never 500.
    """
    if not payload.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")
    library = [
        {
            "id": t["id"],
            "title": t["title"],
            "category": t["category"],
            "estimated_minutes": t["estimated_minutes"],
            "proof_type": t["default_proof_type"],
            "required_staff": t["required_staff"],
        }
        for t in await crud.list_templates(session, q=None, category=None)
        if t["is_active"] and not t["is_bonus"]
    ]
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "library_task_id": {"type": "integer"},
                        "title": {"type": "string"},
                        "item_is_mandatory": {"type": "boolean"},
                    },
                },
            }
        },
    }
    system = (
        "You draft restaurant task checklists. Only suggest tasks from the provided "
        "library or propose new non-bonus tasks. Never propose money/bonus. Return JSON "
        "matching the schema."
    )
    try:
        completion = await llm.complete(
            f"Prompt: {payload.prompt}\nLibrary: {service.json_dump(library)}",
            system_prompt=system,
            schema=schema,
        )
        raw = completion.raw if isinstance(completion.raw, dict) else {}
        items = raw.get("items", [])
        if not isinstance(items, list):
            items = []
    except (AdapterError, ValueError, KeyError):
        items = []
    return {"items": items}


@router.get("/tasks/proof-image")
async def proof_image(
    _admin: TasksAdmin,
    storage: StorageDep,
    path: str = Query(..., description="stored proof object path (tasks/…)"),
) -> Response:
    """Proxy-stream a proof/task photo through the backend (the bucket blocks public
    access + signed-URL signing on Cloud Run — mirror the forms-PDF posture)."""
    object_path = path.split("?", 1)[0].lstrip("/")
    if not object_path.startswith("tasks/"):
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
# STAFF-SELF — CHECKLIST RUNS (require_principal + ownership)
# ══════════════════════════════════════════════════════════════════


@router.get("/staff/checklist-runs")
async def staff_list_runs(
    principal: PrincipalDep,
    session: DbDep,
    venue_id: VenueDep,
    run_date: str | None = Query(None, alias="date"),
) -> dict:
    emp = await _resolve_employee(principal, session)
    d = _parse_date_or_today(run_date)
    clocked = await crud.is_clocked_in(session, employee_id=emp["id"], d=d)
    runs = await crud.get_staff_runs(session, employee_id=emp["id"], d=d, venue_id=venue_id)
    return {"date": d, "clocked_in": clocked, "runs": runs}


@router.get("/staff/checklist-runs/{run_id}")
async def staff_run_detail(
    run_id: int, principal: PrincipalDep, session: DbDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    today = date.today().isoformat()
    clocked = await crud.is_clocked_in(session, employee_id=emp["id"], d=today)
    run = await crud.get_staff_run(session, employee_id=emp["id"], run_id=run_id)
    return {"clocked_in": clocked, **run}


@router.post("/staff/checklist-runs/tasks/{run_task_id}/start")
async def staff_start(
    run_task_id: int, principal: PrincipalDep, session: DbDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    clocked = await crud.is_clocked_in(session, employee_id=emp["id"], d=date.today().isoformat())
    await crud.start_task(session, run_task_id=run_task_id, employee_id=emp["id"], clocked_in=clocked)
    return await crud.staff_task_response(session, run_task_id=run_task_id, employee_id=emp["id"])


@router.post("/staff/checklist-runs/tasks/{run_task_id}/finish")
async def staff_finish(
    run_task_id: int,
    principal: PrincipalDep,
    session: DbDep,
    storage: StorageDep,
    note: str | None = None,
    photos: list[UploadFile] | None = File(None),
) -> dict:
    emp = await _resolve_employee(principal, session)
    clocked = await crud.is_clocked_in(session, employee_id=emp["id"], d=date.today().isoformat())
    incoming = [f for f in (photos or []) if f and f.filename]
    if len(incoming) > service.MAX_PROOF_PHOTOS:
        raise HTTPException(status_code=400, detail=f"too many photos (max {service.MAX_PROOF_PHOTOS})")
    photo_urls: list[str] = []
    for f in incoming:
        data = await f.read()
        if len(data) > MAX_UPLOAD_SIZE:
            raise HTTPException(status_code=413, detail="file too large (max 8MB)")
        key = f"tasks/proofs/{run_task_id}/{f.filename}"
        photo_urls.append(await storage.put(key, data, f.content_type or "application/octet-stream"))
    await crud.finish_task(
        session,
        run_task_id=run_task_id,
        employee_id=emp["id"],
        clocked_in=clocked,
        note=note,
        photo_urls=photo_urls,
    )
    return await crud.staff_task_response(session, run_task_id=run_task_id, employee_id=emp["id"])


@router.post("/staff/checklist-runs/tasks/{run_task_id}/finish-json")
async def staff_finish_json(
    run_task_id: int, payload: FinishPayload, principal: PrincipalDep, session: DbDep
) -> dict:
    """JSON variant of finish for already-hosted proof urls (no multipart upload)."""
    emp = await _resolve_employee(principal, session)
    clocked = await crud.is_clocked_in(session, employee_id=emp["id"], d=date.today().isoformat())
    await crud.finish_task(
        session,
        run_task_id=run_task_id,
        employee_id=emp["id"],
        clocked_in=clocked,
        note=payload.note,
        photo_urls=payload.photos,
    )
    return await crud.staff_task_response(session, run_task_id=run_task_id, employee_id=emp["id"])


@router.post("/staff/checklist-runs/tasks/{run_task_id}/unstart")
async def staff_unstart(
    run_task_id: int, principal: PrincipalDep, session: DbDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    clocked = await crud.is_clocked_in(session, employee_id=emp["id"], d=date.today().isoformat())
    await crud.unstart_task(session, run_task_id=run_task_id, employee_id=emp["id"], clocked_in=clocked)
    return await crud.staff_task_response(session, run_task_id=run_task_id, employee_id=emp["id"])


@router.post("/staff/checklist-runs/tasks/{run_task_id}/subtask")
async def staff_set_subtask(
    run_task_id: int, payload: SubtaskPayload, principal: PrincipalDep, session: DbDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    clocked = await crud.is_clocked_in(session, employee_id=emp["id"], d=date.today().isoformat())
    result = await crud.set_subtask(
        session,
        run_task_id=run_task_id,
        employee_id=emp["id"],
        clocked_in=clocked,
        index=payload.index,
        done=payload.done,
    )
    return {"ok": True, **result}


@router.post("/staff/checklist-runs/tasks/{run_task_id}/take-over")
async def staff_take_over(
    run_task_id: int, payload: TakeOverPayload, principal: PrincipalDep, session: DbDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    clocked = await crud.is_clocked_in(session, employee_id=emp["id"], d=date.today().isoformat())
    await crud.take_over_task(
        session,
        run_task_id=run_task_id,
        employee_id=emp["id"],
        clocked_in=clocked,
        from_employee_id=payload.from_employee_id,
    )
    return await crud.staff_task_response(session, run_task_id=run_task_id, employee_id=emp["id"])


__all__ = ["router"]
