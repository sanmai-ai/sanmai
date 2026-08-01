"""Forms / courses / flows / assignments router — thin, seam-gated endpoints.

Three access tiers:

* **admin authoring** — DB-RBAC via :func:`require_admin` on the ``forms`` page:
  template / course / flow authoring + publishing and assignment management.
* **admin PII / signed-document** — :func:`require_admin` on the ``hr`` page: the
  form-response list + response-PDF reads. These endpoints were **fully
  unauthenticated** in the live system (anyone with the URL streamed a signed
  ID/bank PDF); gating them on ``hr`` is the regression fix — see the tests.
* **staff-self** — :class:`PrincipalDep` verifies the token; the calling *employee*
  is resolved from the VERIFIED identity (email/phone claims) via
  ``hr.crud.find_employee_by_identity`` — NEVER a query-param/body email. Ownership
  is then enforced: a staff member may only read/write THEIR OWN response / draft /
  PDF / assignment / course-progress.

Vendor access is via ADAPTERS only: document bytes through :class:`Storage`
(proxy-streamed, never signed — the Cloud Run SA has no signing key); optional AI
form-field extraction through :class:`LLMProvider` (degrade, never 500). PDF rendering
is behind the :class:`PdfRenderer` seam (Null default — see ``render.py``); no
WeasyPrint in core.

DEFERRED (noted, not built): real WeasyPrint rendering (seamed); Telegram/notify on
nudge (``last_nudged_at`` recorded, no notifier); bilingual legal logic beyond generic
i18n passthrough. No TEST_MODE identity bypass exists — auth is DB-RBAC + verified
token, exercised with real fakes in tests.
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import text

from be.adapters.errors import AdapterError, ProviderPermanent
from be.adapters.llm.base import LLMProvider
from be.adapters.storage.base import Storage
from be.adapters.types import ImagePart, Principal
from be.app.deps import (
    AdminContext,
    DbDep,
    PrincipalDep,
    VenueDep,
    get_llm,
    get_storage,
    require_admin,
)
from be.app.domains.forms import crud, service
from be.app.domains.forms.render import PdfRenderer, get_pdf_renderer
from be.app.domains.forms.schemas import (
    AssignmentCreate,
    CourseCreate,
    CourseItemCompleteRequest,
    CourseUpdate,
    FlowCreate,
    FlowItemAdd,
    FlowItemReorder,
    FlowUpdate,
    GenerateFromFileRequest,
    GenerateFromPromptRequest,
    ItemCreate,
    SaveDraftRequest,
    SectionCreate,
    SignResponseRequest,
    StartResponseRequest,
    SubmitNoSigRequest,
    TemplateCreate,
    TemplateUpdate,
    TranslateRequest,
)
from be.app.domains.hr import crud as hr_crud
from be.app.domains.hr import service as hr_service

router = APIRouter(tags=["forms"])

FormsAdmin = Annotated[AdminContext, Depends(require_admin("forms"))]
HrAdmin = Annotated[AdminContext, Depends(require_admin("hr"))]
StorageDep = Annotated[Storage, Depends(get_storage)]
LLMDep = Annotated[LLMProvider, Depends(get_llm)]
PdfRendererDep = Annotated[PdfRenderer, Depends(get_pdf_renderer)]

_FORM_FIELDS_SCHEMA = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "label": {"type": "string"},
                    "name": {"type": "string"},
                    "required": {"type": "boolean"},
                },
            },
        }
    },
}


# --- helpers ----------------------------------------------------------------


async def _resolve_employee(principal: Principal, session: DbDep) -> dict:
    """Resolve the calling employee from the VERIFIED token identity, or 404."""
    email = hr_service.normalize_email(principal.claims.get("email"))
    try:
        phone = hr_service.normalize_phone(
            principal.claims.get("phone") or principal.claims.get("phone_number")
        )
    except ValueError:
        phone = None
    emp = await hr_crud.find_employee_by_identity(session, email=email, phone=phone)
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return emp


async def _resolve_admin_employee_id(admin: AdminContext, session: DbDep) -> int | None:
    """Best-effort: map the acting admin to an employee id for ``assigned_by`` (or None)."""
    email = hr_service.normalize_email(admin.email)
    if not email:
        return None
    emp = await hr_crud.find_employee_by_identity(session, email=email, phone=None)
    return emp["id"] if emp else None


def _client_ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    if request.client and request.client.host:
        return request.client.host
    return None


async def _stream_response_pdf(
    session: DbDep,
    storage: Storage,
    renderer: PdfRenderer,
    response: dict,
    response_id: str,
) -> Response:
    """Proxy-stream a response PDF, rendering on demand via the seam if missing."""
    path = response.get("pdf_gcs_path")
    if not path:
        path = await _ensure_response_pdf(session, storage, renderer, response)
    if not path:
        raise HTTPException(status_code=404, detail="PDF not available")
    try:
        data = await storage.get(path)
    except (AdapterError, FileNotFoundError, KeyError, ProviderPermanent) as exc:
        raise HTTPException(status_code=404, detail="PDF not found") from exc
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="response-{response_id}.pdf"'},
    )


async def _ensure_response_pdf(
    session: DbDep, storage: Storage, renderer: PdfRenderer, response: dict
) -> str | None:
    """Render (via the seam) + store a response PDF if absent. Returns the key or None."""
    if response.get("pdf_gcs_path"):
        return response["pdf_gcs_path"]
    template = await crud.get_template(session, response["template_id"])
    employee = await crud.get_employee_for_pdf(session, response["employee_id"])
    if not template or not employee:
        return None
    rendered = renderer.render(template, response, employee)
    if rendered is None:
        return None  # DEFERRED: Null renderer — no artifact yet
    pdf_bytes, sha256_hex = rendered
    key = f"forms/{response['employee_id']}/{response['id']}/{service.now_iso()}.pdf"
    stored = await storage.put(key, pdf_bytes, "application/pdf")
    await crud.set_pdf_for_response(
        session, response["id"], pdf_gcs_path=stored, pdf_sha256=sha256_hex
    )
    return stored


# ══════════════════════════════════════════════════════════════════
# ADMIN — FORMS TEMPLATES (require_admin("forms"))
# ══════════════════════════════════════════════════════════════════


@router.get("/admin/forms/templates")
async def list_templates(
    _admin: FormsAdmin, session: DbDep, active_only: bool = Query(False)
) -> list[dict]:
    return await crud.list_templates(session, active_only=active_only)


@router.post("/admin/forms/templates")
async def create_template(
    payload: TemplateCreate, admin: FormsAdmin, session: DbDep, venue_id: VenueDep
) -> dict:
    try:
        service.validate_binding_language(payload.binding_language)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    created_by = await _resolve_admin_employee_id(admin, session)
    tpl = await crud.create_template(
        session,
        name_he=payload.name_he,
        name_en=payload.name_en,
        description_he=payload.description_he,
        description_en=payload.description_en,
        fields=payload.fields,
        binding_language=payload.binding_language,
        bilingual=payload.bilingual,
        created_by=created_by,
        venue_id=venue_id,
    )
    return {"id": tpl["id"]}


@router.get("/admin/forms/templates/{template_id}")
async def get_template(template_id: str, _admin: FormsAdmin, session: DbDep) -> dict:
    tpl = await crud.get_template(session, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="template not found")
    return tpl


@router.patch("/admin/forms/templates/{template_id}")
async def update_template(
    template_id: str, payload: TemplateUpdate, _admin: FormsAdmin, session: DbDep
) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    try:
        service.validate_binding_language(fields.get("binding_language"))
        return await crud.update_template(session, template_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except crud.TemplateNotFound as exc:
        raise HTTPException(status_code=404, detail="template not found") from exc
    except crud.TemplateNotDraft as exc:
        raise HTTPException(status_code=409, detail="only draft templates can be updated") from exc


@router.delete("/admin/forms/templates/{template_id}")
async def delete_template(template_id: str, _admin: FormsAdmin, session: DbDep) -> dict:
    try:
        await crud.delete_template(session, template_id)
    except crud.TemplateNotFound as exc:
        raise HTTPException(status_code=404, detail="template not found") from exc
    except crud.TemplateInUse as exc:
        raise HTTPException(
            status_code=409, detail="template has responses; archive instead of delete"
        ) from exc
    return {"ok": True}


@router.post("/admin/forms/templates/{template_id}/publish")
async def publish_template(template_id: str, _admin: FormsAdmin, session: DbDep) -> dict:
    try:
        return await crud.publish_template(session, template_id)
    except crud.TemplateNotFound as exc:
        raise HTTPException(status_code=404, detail="template not found") from exc
    except crud.TemplateNotDraft as exc:
        raise HTTPException(status_code=409, detail="template is not a draft") from exc


@router.post("/admin/forms/templates/{template_id}/archive")
async def archive_template(template_id: str, _admin: FormsAdmin, session: DbDep) -> dict:
    try:
        return await crud.archive_template(session, template_id)
    except crud.TemplateNotFound as exc:
        raise HTTPException(status_code=404, detail="template not found") from exc


@router.post("/admin/forms/templates/{template_id}/new-draft")
async def new_draft_from_template(template_id: str, _admin: FormsAdmin, session: DbDep) -> dict:
    try:
        new_id = await crud.clone_published_to_draft(session, template_id)
    except crud.TemplateNotFound as exc:
        raise HTTPException(status_code=404, detail="template not found") from exc
    except crud.TemplateArchived as exc:
        raise HTTPException(status_code=409, detail="archived templates cannot be edited") from exc
    return {"id": new_id}


# --- AI authoring seams (LLMProvider; degrade, never 500) --------------------


@router.post("/admin/forms/templates/generate-from-prompt")
async def generate_form_from_prompt(
    payload: GenerateFromPromptRequest, _admin: FormsAdmin, llm: LLMDep
) -> dict:
    if not payload.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")
    system = (
        "You draft structured form definitions. Return JSON matching the schema: a "
        "'fields' array where each field has type/label/name/required. Language: "
        f"{payload.language}."
    )
    return {"fields": await _extract_fields(llm, payload.prompt, system, images=None)}


@router.post("/admin/forms/templates/generate-from-file")
async def generate_form_from_file(
    payload: GenerateFromFileRequest, _admin: FormsAdmin, llm: LLMDep
) -> dict:
    if len(payload.file_base64) > 7_000_000:
        raise HTTPException(status_code=413, detail="file too large (max ~5MB)")
    try:
        raw = base64.b64decode(payload.file_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="file_base64 is not valid base64") from exc
    system = (
        "Extract a structured form definition from the attached document. Return JSON "
        "matching the schema: a 'fields' array (type/label/name/required). Language: "
        f"{payload.language}."
    )
    image = ImagePart(data=raw, mime_type=payload.mime_type)
    return {"fields": await _extract_fields(llm, payload.prompt or "Extract the form fields.", system, images=[image])}


@router.post("/admin/forms/templates/{template_id}/translate")
async def translate_template(
    template_id: str, payload: TranslateRequest, _admin: FormsAdmin, llm: LLMDep
) -> dict:
    if payload.source == payload.target:
        raise HTTPException(status_code=400, detail="source and target languages must differ")
    system = (
        f"Translate the form content from {payload.source} to {payload.target}. Preserve "
        "the field structure. Return JSON: {name, fields}."
    )
    prompt = service.json_dump({"name": payload.name, "fields": payload.fields})
    try:
        completion = await llm.complete(
            prompt,
            system_prompt=system,
            schema={
                "type": "object",
                "properties": {"name": {"type": "string"}, "fields": {"type": "array"}},
            },
        )
        raw = completion.raw if isinstance(completion.raw, dict) else {}
    except (AdapterError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=502, detail="translation failed") from exc
    return {"name": raw.get("name", payload.name), "fields": raw.get("fields", [])}


async def _extract_fields(
    llm: LLMProvider, prompt: str, system: str, *, images: list[ImagePart] | None
) -> list:
    try:
        completion = await llm.complete(
            prompt, system_prompt=system, schema=_FORM_FIELDS_SCHEMA, images=images
        )
        raw = completion.raw if isinstance(completion.raw, dict) else {}
        fields = raw.get("fields", [])
        return fields if isinstance(fields, list) else []
    except (AdapterError, ValueError, KeyError):
        return []


# ══════════════════════════════════════════════════════════════════
# ADMIN — PII / SIGNED DOCUMENTS (require_admin("hr"))  ← fixes live leak
# ══════════════════════════════════════════════════════════════════


@router.get("/admin/forms/templates/{template_id}/responses")
async def list_template_responses(template_id: str, _admin: HrAdmin, session: DbDep) -> list[dict]:
    return await crud.list_responses_for_template(session, template_id)


@router.get("/admin/forms/responses/{response_id}/pdf-url")
async def admin_response_pdf_url(response_id: str, _admin: HrAdmin, request: Request) -> dict:
    base = str(request.base_url).replace("http://", "https://", 1)
    return {"url": f"{base}api/v1/admin/forms/responses/{response_id}/pdf"}


@router.get("/admin/forms/responses/{response_id}/pdf")
async def admin_response_pdf(
    response_id: str,
    _admin: HrAdmin,
    session: DbDep,
    storage: StorageDep,
    renderer: PdfRendererDep,
) -> Response:
    response = await crud.get_response(session, response_id)
    if not response:
        raise HTTPException(status_code=404, detail="response not found")
    return await _stream_response_pdf(session, storage, renderer, response, response_id)


# ══════════════════════════════════════════════════════════════════
# ADMIN — COURSES (require_admin("forms"))
# ══════════════════════════════════════════════════════════════════


@router.get("/admin/forms/courses")
async def list_courses(_admin: FormsAdmin, session: DbDep) -> list[dict]:
    return await crud.list_courses(session)


@router.post("/admin/forms/courses")
async def create_course(
    payload: CourseCreate, admin: FormsAdmin, session: DbDep, venue_id: VenueDep
) -> dict:
    created_by = await _resolve_admin_employee_id(admin, session)
    new_id = await crud.create_course(
        session,
        name_he=payload.name_he,
        name_en=payload.name_en,
        description_he=payload.description_he,
        description_en=payload.description_en,
        created_by=created_by,
        venue_id=venue_id,
    )
    return {"id": new_id}


@router.get("/admin/forms/courses/{course_id}")
async def get_course(course_id: str, _admin: FormsAdmin, session: DbDep) -> dict:
    course = await crud.get_course(session, course_id)
    if not course:
        raise HTTPException(status_code=404, detail="course not found")
    return course


@router.patch("/admin/forms/courses/{course_id}")
async def update_course(
    course_id: str, payload: CourseUpdate, _admin: FormsAdmin, session: DbDep
) -> dict:
    try:
        return await crud.update_course(session, course_id, **payload.model_dump(exclude_unset=True))
    except crud.CourseNotFound as exc:
        raise HTTPException(status_code=404, detail="course not found") from exc
    except crud.CourseNotDraft as exc:
        raise HTTPException(status_code=409, detail="only draft courses can be updated") from exc


@router.delete("/admin/forms/courses/{course_id}")
async def delete_course(course_id: str, _admin: FormsAdmin, session: DbDep) -> dict:
    try:
        await crud.delete_course(session, course_id)
    except crud.CourseNotFound as exc:
        raise HTTPException(status_code=404, detail="course not found") from exc
    return {"ok": True}


@router.post("/admin/forms/courses/{course_id}/publish")
async def publish_course(course_id: str, _admin: FormsAdmin, session: DbDep) -> dict:
    try:
        return await crud.publish_course(session, course_id)
    except crud.CourseNotFound as exc:
        raise HTTPException(status_code=404, detail="course not found") from exc
    except crud.CourseNotDraft as exc:
        raise HTTPException(status_code=409, detail="course is not a draft") from exc


@router.post("/admin/forms/courses/{course_id}/sections")
async def add_course_section(
    course_id: str, payload: SectionCreate, _admin: FormsAdmin, session: DbDep
) -> dict:
    section_id = await crud.add_section(
        session, course_id, title_he=payload.title_he, title_en=payload.title_en, position=payload.position
    )
    return {"id": section_id}


@router.post("/admin/forms/courses/sections/{section_id}/items")
async def add_course_item(
    section_id: str, payload: ItemCreate, _admin: FormsAdmin, session: DbDep
) -> dict:
    try:
        service.validate_course_item_type(payload.type)
        item_id = await crud.add_item(
            session, section_id, type=payload.type, payload=payload.payload, position=payload.position
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except crud.SectionNotFound as exc:
        raise HTTPException(status_code=404, detail="section not found") from exc
    return {"id": item_id}


# ══════════════════════════════════════════════════════════════════
# ADMIN — FLOWS (require_admin("forms"))
# ══════════════════════════════════════════════════════════════════


@router.get("/admin/forms/flows")
async def list_flows(
    _admin: FormsAdmin, session: DbDep, active_only: bool = Query(False)
) -> list[dict]:
    return await crud.list_flows(session, active_only=active_only)


@router.post("/admin/forms/flows")
async def create_flow(
    payload: FlowCreate, _admin: FormsAdmin, session: DbDep, venue_id: VenueDep
) -> dict:
    try:
        service.validate_flow_ordering(payload.ordering)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    new_id = await crud.create_flow(
        session,
        name_he=payload.name_he,
        name_en=payload.name_en,
        ordering=payload.ordering,
        default_due_days=payload.default_due_days,
        created_by=None,
        venue_id=venue_id,
    )
    return {"id": new_id}


@router.get("/admin/forms/flows/{flow_id}")
async def get_flow(flow_id: str, _admin: FormsAdmin, session: DbDep) -> dict:
    flow = await crud.get_flow(session, flow_id)
    if not flow:
        raise HTTPException(status_code=404, detail="flow not found")
    return flow


@router.patch("/admin/forms/flows/{flow_id}")
async def update_flow(
    flow_id: str, payload: FlowUpdate, _admin: FormsAdmin, session: DbDep
) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    try:
        service.validate_flow_ordering(fields.get("ordering"))
        return await crud.update_flow(session, flow_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except crud.FlowNotFound as exc:
        raise HTTPException(status_code=404, detail="flow not found") from exc
    except crud.FlowNotDraft as exc:
        raise HTTPException(status_code=409, detail="only draft flows can be updated") from exc


@router.post("/admin/forms/flows/{flow_id}/publish")
async def publish_flow(flow_id: str, _admin: FormsAdmin, session: DbDep) -> dict:
    try:
        return await crud.publish_flow(session, flow_id)
    except crud.FlowNotFound as exc:
        raise HTTPException(status_code=404, detail="flow not found") from exc
    except crud.FlowNotDraft as exc:
        raise HTTPException(status_code=409, detail="flow is not a draft") from exc


@router.post("/admin/forms/flows/{flow_id}/items")
async def add_flow_item(
    flow_id: str, payload: FlowItemAdd, _admin: FormsAdmin, session: DbDep
) -> dict:
    try:
        service.validate_flow_item_type(payload.item_type)
        item_id = await crud.add_flow_item(
            session,
            flow_id,
            position=payload.position,
            item_type=payload.item_type,
            item_id=payload.item_id,
            due_days_override=payload.due_days_override,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except crud.FlowNotFound as exc:
        raise HTTPException(status_code=404, detail="flow not found") from exc
    except crud.FlowNotDraft as exc:
        raise HTTPException(status_code=409, detail="only draft flows can have items added") from exc
    except crud.FlowItemTargetMissing as exc:
        raise HTTPException(status_code=404, detail=f"target {payload.item_type} not found") from exc
    return {"id": item_id}


@router.delete("/admin/forms/flows/items/{item_id}")
async def remove_flow_item(item_id: str, _admin: FormsAdmin, session: DbDep) -> dict:
    try:
        await crud.remove_flow_item(session, item_id)
    except crud.FlowItemNotFound as exc:
        raise HTTPException(status_code=404, detail="flow item not found") from exc
    return {"ok": True}


@router.put("/admin/forms/flows/{flow_id}/items/order")
async def reorder_flow_items(
    flow_id: str, payload: FlowItemReorder, _admin: FormsAdmin, session: DbDep
) -> dict:
    try:
        await crud.reorder_flow_items(session, flow_id, payload.item_ids)
    except crud.FlowNotFound as exc:
        raise HTTPException(status_code=404, detail="flow not found") from exc
    except crud.FlowNotDraft as exc:
        raise HTTPException(status_code=409, detail="only draft flows can be reordered") from exc
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# ADMIN — ASSIGNMENTS (require_admin("forms"))
# ══════════════════════════════════════════════════════════════════


@router.post("/admin/forms/assignments")
async def create_assignment(
    payload: AssignmentCreate, admin: FormsAdmin, session: DbDep, venue_id: VenueDep
) -> dict:
    emp_ids = payload.employee_id if isinstance(payload.employee_id, list) else [payload.employee_id]
    if not emp_ids:
        raise HTTPException(status_code=400, detail="employee_id required (single or list)")
    assigned_by = await _resolve_admin_employee_id(admin, session)
    created: list[str] = []
    try:
        for eid in emp_ids:
            new_id = await crud.create_assignment(
                session,
                employee_id=eid,
                source=payload.source,
                flow_id=payload.flow_id,
                form_id=payload.form_id,
                course_id=payload.course_id,
                assigned_by=assigned_by,
                due_at=payload.due_at,
                venue_id=venue_id,
            )
            created.append(new_id)
    except crud.AssignmentSourceInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ids": created, "count": len(created)}


@router.get("/admin/forms/assignments")
async def list_assignments(
    _admin: FormsAdmin,
    session: DbDep,
    employee_id: int | None = Query(None),
    status: str | None = Query(None),
    sort: str = Query("due_at_asc"),
) -> list[dict]:
    return await crud.list_assignments(
        session,
        employee_id=employee_id,
        status_filter=service.normalize_assignment_status(status),
        sort=service.normalize_assignment_sort(sort),
    )


@router.get("/admin/forms/assignments/{assignment_id}")
async def get_assignment(assignment_id: str, _admin: FormsAdmin, session: DbDep) -> dict:
    assignment = await crud.get_assignment(session, assignment_id)
    if not assignment:
        raise HTTPException(status_code=404, detail="assignment not found")
    return assignment


@router.post("/admin/forms/assignments/{assignment_id}/nudge")
async def nudge_assignment(assignment_id: str, _admin: FormsAdmin, session: DbDep) -> dict:
    try:
        return await crud.nudge_assignment(session, assignment_id)
    except crud.AssignmentNotFound as exc:
        raise HTTPException(status_code=404, detail="assignment not found") from exc


@router.delete("/admin/forms/assignments/{assignment_id}")
async def delete_assignment(assignment_id: str, _admin: FormsAdmin, session: DbDep) -> dict:
    if not await crud.delete_assignment(session, assignment_id):
        raise HTTPException(status_code=404, detail="assignment not found")
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# STAFF-SELF (require_principal + ownership vs the VERIFIED token)
# ══════════════════════════════════════════════════════════════════


@router.get("/staff/forms/{template_id}")
async def staff_get_form(
    template_id: str,
    principal: PrincipalDep,
    session: DbDep,
    response_id: str | None = Query(None),
) -> dict:
    emp = await _resolve_employee(principal, session)
    template = await crud.get_published_template_by_parent(session, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="published template not found")

    draft = None
    if response_id is not None:
        resp = await crud.get_response(session, response_id)
        if not resp:
            raise HTTPException(status_code=404, detail="draft not found")
        if resp["employee_id"] != emp["id"]:
            raise HTTPException(status_code=403, detail="cannot access another employee's draft")
        if resp["status"] == "draft":
            draft = {
                "response_id": resp["id"],
                "answers": resp["answers"] or {},
                "updated_at": resp["updated_at"],
            }
    else:
        draft = await crud.get_latest_draft(session, employee_id=emp["id"], template_id=template_id)
    return {"template": template, "draft": draft}


@router.post("/staff/form-responses")
async def staff_start_response(
    payload: StartResponseRequest, principal: PrincipalDep, session: DbDep, venue_id: VenueDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    try:
        response_id = await crud.start_response(
            session,
            template_id=payload.template_id,
            employee_id=emp["id"],
            assignment_progress_id=payload.assignment_progress_id,
            venue_id=venue_id,
        )
    except crud.TemplateNotPublished as exc:
        raise HTTPException(status_code=404, detail="published template not found") from exc
    return {"id": response_id}


async def _load_owned_response(session: DbDep, response_id: str, employee_id: int) -> dict:
    """Load a response and enforce ownership (403 for another employee's row)."""
    resp = await crud.get_response(session, response_id)
    if not resp:
        raise HTTPException(status_code=404, detail="response not found")
    if resp["employee_id"] != employee_id:
        raise HTTPException(status_code=403, detail="cannot access another employee's response")
    return resp


@router.patch("/staff/form-responses/{response_id}")
async def staff_save_draft(
    response_id: str, payload: SaveDraftRequest, principal: PrincipalDep, session: DbDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    await _load_owned_response(session, response_id, emp["id"])  # closes the no-identity hole
    try:
        return await crud.save_response_draft(session, response_id, payload.answers)
    except crud.ResponseNotFound as exc:
        raise HTTPException(status_code=404, detail="response not found") from exc
    except crud.ResponseNotDraft as exc:
        raise HTTPException(status_code=409, detail="response is not a draft") from exc


@router.get("/staff/signed-docs")
async def staff_list_signed_docs(principal: PrincipalDep, session: DbDep) -> list[dict]:
    emp = await _resolve_employee(principal, session)
    return await crud.list_responses_for_employee(session, emp["id"], status="submitted")


@router.post("/staff/form-responses/{response_id}/submit")
async def staff_submit_no_signature(
    response_id: str,
    payload: SubmitNoSigRequest,
    request: Request,
    principal: PrincipalDep,
    session: DbDep,
) -> dict:
    emp = await _resolve_employee(principal, session)
    resp = await _load_owned_response(session, response_id, emp["id"])
    if resp["status"] == "submitted":
        raise HTTPException(status_code=409, detail="response already submitted")
    template = await crud.get_template(session, resp["template_id"])
    if not template:
        raise HTTPException(status_code=404, detail="template not found")
    if service.template_has_signature_field(template["fields"]):
        raise HTTPException(
            status_code=400,
            detail="template has a signature field — use /sign to produce a signed PDF",
        )
    try:
        await crud.save_response_draft(session, response_id, payload.answers)
        submitted = await crud.submit_response(
            session,
            response_id,
            signature_base64=None,
            binding_language_confirmed=None,
            submitted_ip=_client_ip(request),
        )
    except crud.ResponseNotFound as exc:
        raise HTTPException(status_code=404, detail="response not found") from exc
    except crud.ResponseNotDraft as exc:
        raise HTTPException(status_code=409, detail="response already submitted") from exc
    await _complete_linked_progress(session, submitted.get("assignment_progress_id"))
    return {"success": True, "response_id": response_id}


@router.post("/staff/form-responses/{response_id}/sign")
async def staff_sign_response(
    response_id: str,
    payload: SignResponseRequest,
    request: Request,
    principal: PrincipalDep,
    session: DbDep,
    storage: StorageDep,
    renderer: PdfRendererDep,
) -> dict:
    emp = await _resolve_employee(principal, session)
    resp = await _load_owned_response(session, response_id, emp["id"])
    if resp["status"] == "submitted":
        raise HTTPException(status_code=409, detail="response already submitted")
    template = await crud.get_template(session, resp["template_id"])
    if not template:
        raise HTTPException(status_code=404, detail="template not found")
    try:
        await crud.save_response_draft(session, response_id, payload.answers)
        submitted = await crud.submit_response(
            session,
            response_id,
            signature_base64=payload.signature_base64,
            binding_language_confirmed=payload.binding_language_confirmed,
            submitted_ip=_client_ip(request),
        )
    except crud.ResponseNotFound as exc:
        raise HTTPException(status_code=404, detail="response not found") from exc
    except crud.ResponseNotDraft as exc:
        raise HTTPException(status_code=409, detail="response already submitted") from exc

    # Render + store the signed PDF via the seam (Null default defers the artifact).
    pdf_sha256 = None
    stored = await _ensure_response_pdf(session, storage, renderer, submitted)
    if stored:
        refreshed = await crud.get_response(session, response_id)
        pdf_sha256 = refreshed["pdf_sha256"] if refreshed else None

    await _complete_linked_progress(session, submitted.get("assignment_progress_id"))
    return {"success": True, "response_id": response_id, "pdf_sha256": pdf_sha256}


async def _complete_linked_progress(session: DbDep, progress_id: str | None) -> None:
    """Best-effort: mark the linked assignment item complete (unlocks the next one)."""
    if not progress_id:
        return
    try:
        await crud.update_progress_status(
            session, progress_id, "completed", completed_at=service.now_iso()
        )
    except crud.ProgressNotFound:
        pass


@router.get("/staff/form-responses/{response_id}/pdf-url")
async def staff_response_pdf_url(
    response_id: str, request: Request, principal: PrincipalDep, session: DbDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    await _load_owned_response(session, response_id, emp["id"])
    base = str(request.base_url).replace("http://", "https://", 1)
    return {"url": f"{base}api/v1/staff/form-responses/{response_id}/pdf"}


@router.get("/staff/form-responses/{response_id}/pdf")
async def staff_response_pdf(
    response_id: str,
    principal: PrincipalDep,
    session: DbDep,
    storage: StorageDep,
    renderer: PdfRendererDep,
) -> Response:
    emp = await _resolve_employee(principal, session)
    resp = await _load_owned_response(session, response_id, emp["id"])
    return await _stream_response_pdf(session, storage, renderer, resp, response_id)


@router.get("/staff/assignments")
async def staff_list_assignments(principal: PrincipalDep, session: DbDep) -> list[dict]:
    emp = await _resolve_employee(principal, session)
    return await crud.list_assignments(session, employee_id=emp["id"], status_filter=None, sort="due_at_asc")


@router.get("/staff/assignments/{assignment_id}")
async def staff_get_assignment(
    assignment_id: str, principal: PrincipalDep, session: DbDep
) -> dict:
    emp = await _resolve_employee(principal, session)
    assignment = await crud.get_assignment(session, assignment_id)
    if not assignment:
        raise HTTPException(status_code=404, detail="assignment not found")
    if assignment["employee_id"] != emp["id"]:
        raise HTTPException(status_code=403, detail="cannot access another employee's assignment")
    return assignment


@router.post("/staff/course-progress/{course_id}/item/{item_id}/complete")
async def staff_complete_course_item(
    course_id: str,
    item_id: str,
    payload: CourseItemCompleteRequest,
    principal: PrincipalDep,
    session: DbDep,
    venue_id: VenueDep,
) -> dict:
    emp = await _resolve_employee(principal, session)
    try:
        result = await crud.complete_course_item(
            session, course_id=course_id, item_id=item_id, employee_id=emp["id"], venue_id=venue_id
        )
    except crud.CourseNotFound as exc:
        raise HTTPException(status_code=404, detail="course not found") from exc

    if result["course_complete"] and payload.assignment_progress_id is not None:
        # Ownership: only flip a progress row that belongs to this employee's assignment.
        if await _progress_belongs_to(session, payload.assignment_progress_id, emp["id"]):
            await _complete_linked_progress(session, payload.assignment_progress_id)
    return {"ok": True, **result}


async def _progress_belongs_to(session: DbDep, progress_id: str, employee_id: int) -> bool:
    row = (
        await session.execute(
            text(
                "SELECT a.employee_id FROM assignment_progress ap "
                "JOIN assignments a ON a.id = ap.assignment_id WHERE ap.id = :pid"
            ),
            {"pid": progress_id},
        )
    ).mappings().first()
    return bool(row and row["employee_id"] == employee_id)


__all__ = ["router"]
