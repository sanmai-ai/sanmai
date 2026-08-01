"""Forms / courses / flows / assignments CRUD — ``text()`` SQL + async functions.

All SQL is dialect-agnostic (sqlite in tests, postgres in prod), mirroring the
tasks/hr domains:

* unqualified table names; TEXT surrogate ids allocated app-side (``service.new_id``);
* booleans stored as INTEGER 0/1; timestamps/dates as TEXT ISO; JSON as TEXT
  (parsed/dumped via :mod:`be.app.domains.forms.service`);
* the completed-item set is a JSON TEXT list de-duped in Python (no postgres ``TEXT[]``);
* id lists use an expanding IN bind (no ``ANY(...)``); no ``ON CONFLICT`` / ``now()``;
* polymorphic ``flow_items.item_id`` existence + cascade deletes are enforced here
  (no cross-table FK / ``ON DELETE CASCADE``);
* the assignment status/overdue rollup + the version-clone publish + the sequential
  unlock are computed in Python (see :mod:`~be.app.domains.forms.service`).

These functions raise the domain exceptions below; the router maps them to HTTP codes.
Ownership (a staff member may only touch their own response/progress/assignment) is
enforced in the router against the VERIFIED token identity — never a query param.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from be.app.domains.forms import service

# --- domain exceptions ------------------------------------------------------


class TemplateNotFound(Exception):
    pass


class TemplateNotDraft(Exception):
    pass


class TemplateInUse(Exception):
    pass


class TemplateArchived(Exception):
    pass


class ResponseNotFound(Exception):
    pass


class ResponseNotDraft(Exception):
    pass


class TemplateNotPublished(Exception):
    pass


class CourseNotFound(Exception):
    pass


class CourseNotDraft(Exception):
    pass


class SectionNotFound(Exception):
    pass


class FlowNotFound(Exception):
    pass


class FlowNotDraft(Exception):
    pass


class FlowItemNotFound(Exception):
    pass


class FlowItemTargetMissing(Exception):
    pass


class AssignmentNotFound(Exception):
    pass


class AssignmentSourceInvalid(Exception):
    pass


class ProgressNotFound(Exception):
    pass


# ══════════════════════════════════════════════════════════════════
# FORMS TEMPLATES
# ══════════════════════════════════════════════════════════════════

_TEMPLATE_COLS = (
    "id, venue_id, name_he, name_en, description_he, description_en, fields, "
    "binding_language, bilingual, status, version, parent_id, created_by, "
    "created_at, published_at"
)

_LIST_TEMPLATES = text(
    f"""
    SELECT {_TEMPLATE_COLS},
           (SELECT COUNT(*) FROM form_responses r WHERE r.template_id = t.id)
             AS latest_response_count
    FROM forms_templates t
    WHERE (CAST(:active_only AS INTEGER) = 0 OR t.status = 'published')
    ORDER BY published_at DESC, created_at DESC
    """  # noqa: S608 - fixed column list
)

_GET_TEMPLATE = text(f"SELECT {_TEMPLATE_COLS} FROM forms_templates WHERE id = :id")  # noqa: S608

_INSERT_TEMPLATE = text(
    f"""
    INSERT INTO forms_templates (
        id, venue_id, name_he, name_en, description_he, description_en, fields,
        binding_language, bilingual, status, version, parent_id, created_by, created_at
    ) VALUES (
        :id, :venue_id, :name_he, :name_en, :description_he, :description_en, :fields,
        :binding_language, :bilingual, 'draft', 1, :id, :created_by, :created_at
    )
    RETURNING {_TEMPLATE_COLS}
    """  # noqa: S608
)

_UPDATABLE_TEMPLATE_FIELDS = {
    "name_he": None,
    "name_en": None,
    "description_he": None,
    "description_en": None,
    "fields": "json",
    "binding_language": None,
    "bilingual": "bool",
}


def _template_row(r: Any) -> dict:
    d = dict(r)
    d["fields"] = service.json_load(d.get("fields"), [])
    d["bilingual"] = bool(d.get("bilingual"))
    return d


async def list_templates(session: AsyncSession, *, active_only: bool = False) -> list[dict]:
    rows = (
        await session.execute(_LIST_TEMPLATES, {"active_only": 1 if active_only else 0})
    ).mappings().all()
    out = []
    for r in rows:
        d = _template_row(r)
        d["latest_response_count"] = r["latest_response_count"]
        out.append(d)
    return out


async def get_template(session: AsyncSession, template_id: str) -> dict | None:
    row = (await session.execute(_GET_TEMPLATE, {"id": template_id})).mappings().first()
    return _template_row(row) if row else None


async def create_template(
    session: AsyncSession,
    *,
    name_he: str,
    name_en: str,
    description_he: str | None,
    description_en: str | None,
    fields: list,
    binding_language: str,
    bilingual: bool,
    created_by: int | None,
    venue_id: str,
) -> dict:
    row = (
        await session.execute(
            _INSERT_TEMPLATE,
            {
                "id": service.new_id(),
                "venue_id": venue_id,
                "name_he": name_he,
                "name_en": name_en,
                "description_he": description_he,
                "description_en": description_en,
                "fields": service.json_dump(fields or []),
                "binding_language": binding_language or "none",
                "bilingual": 1 if bilingual else 0,
                "created_by": created_by,
                "created_at": service.now_iso(),
            },
        )
    ).mappings().one()
    await session.commit()
    return _template_row(row)


async def update_template(session: AsyncSession, template_id: str, **fields_to_update) -> dict:
    current = await get_template(session, template_id)
    if not current:
        raise TemplateNotFound
    if current["status"] != "draft":
        raise TemplateNotDraft

    sets: list[str] = []
    params: dict[str, Any] = {"id": template_id}
    for key, value in fields_to_update.items():
        if value is None or key not in _UPDATABLE_TEMPLATE_FIELDS:
            continue
        transform = _UPDATABLE_TEMPLATE_FIELDS[key]
        sets.append(f"{key} = :{key}")
        if transform == "json":
            params[key] = service.json_dump(value)
        elif transform == "bool":
            params[key] = 1 if value else 0
        else:
            params[key] = value
    if not sets:
        return current
    await session.execute(
        text(f"UPDATE forms_templates SET {', '.join(sets)} WHERE id = :id"),  # noqa: S608
        params,
    )
    await session.commit()
    updated = await get_template(session, template_id)
    assert updated is not None  # noqa: S101 - just updated
    return updated


_FIND_PREV_PUBLISHED_TPL = text(
    "SELECT id, version FROM forms_templates "
    "WHERE parent_id = :parent AND status = 'published' ORDER BY version DESC LIMIT 1"
)
_ARCHIVE_TPL_BY_ID = text("UPDATE forms_templates SET status = 'archived' WHERE id = :id")


async def publish_template(session: AsyncSession, template_id: str) -> dict:
    current = await get_template(session, template_id)
    if not current:
        raise TemplateNotFound
    parent_id = current["parent_id"] or current["id"]
    prev = (
        await session.execute(_FIND_PREV_PUBLISHED_TPL, {"parent": parent_id})
    ).mappings().first()

    if prev and prev["id"] != current["id"]:
        new_version = prev["version"] + 1
        cloned = await _clone_template_version(session, current, new_version)
        await session.execute(_ARCHIVE_TPL_BY_ID, {"id": prev["id"]})
        await session.execute(_ARCHIVE_TPL_BY_ID, {"id": template_id})
        await session.commit()
        return cloned

    if current["status"] != "draft":
        raise TemplateNotDraft
    now = service.now_iso()
    await session.execute(
        text(
            "UPDATE forms_templates SET status = 'published', published_at = :now WHERE id = :id"
        ),
        {"now": now, "id": template_id},
    )
    await session.commit()
    updated = await get_template(session, template_id)
    assert updated is not None  # noqa: S101
    return updated


async def _clone_template_version(session: AsyncSession, source: dict, new_version: int) -> dict:
    new_id = service.new_id()
    now = service.now_iso()
    await session.execute(
        _INSERT_TEMPLATE_CLONE,
        {
            "id": new_id,
            "venue_id": source["venue_id"],
            "name_he": source["name_he"],
            "name_en": source["name_en"],
            "description_he": source["description_he"],
            "description_en": source["description_en"],
            "fields": service.json_dump(source["fields"]),
            "binding_language": source["binding_language"],
            "bilingual": 1 if source["bilingual"] else 0,
            "status": "published",
            "version": new_version,
            "parent_id": source["parent_id"] or source["id"],
            "created_by": source["created_by"],
            "created_at": now,
            "published_at": now,
        },
    )
    cloned = await get_template(session, new_id)
    assert cloned is not None  # noqa: S101
    return cloned


_INSERT_TEMPLATE_CLONE = text(
    """
    INSERT INTO forms_templates (
        id, venue_id, name_he, name_en, description_he, description_en, fields,
        binding_language, bilingual, status, version, parent_id, created_by,
        created_at, published_at
    ) VALUES (
        :id, :venue_id, :name_he, :name_en, :description_he, :description_en, :fields,
        :binding_language, :bilingual, :status, :version, :parent_id, :created_by,
        :created_at, :published_at
    )
    """
)


async def archive_template(session: AsyncSession, template_id: str) -> dict:
    current = await get_template(session, template_id)
    if not current:
        raise TemplateNotFound
    await session.execute(_ARCHIVE_TPL_BY_ID, {"id": template_id})
    await session.commit()
    updated = await get_template(session, template_id)
    assert updated is not None  # noqa: S101
    return updated


_FIND_OPEN_DRAFT_TPL = text(
    "SELECT id FROM forms_templates WHERE parent_id = :parent AND status = 'draft' "
    "ORDER BY created_at DESC LIMIT 1"
)


async def clone_published_to_draft(session: AsyncSession, template_id: str) -> str:
    source = await get_template(session, template_id)
    if source is None:
        raise TemplateNotFound
    if source["status"] == "draft":
        return source["id"]
    if source["status"] == "archived":
        raise TemplateArchived
    parent_id = source["parent_id"] or source["id"]
    existing = (
        await session.execute(_FIND_OPEN_DRAFT_TPL, {"parent": parent_id})
    ).mappings().first()
    if existing:
        return existing["id"]
    new_id = service.new_id()
    await session.execute(
        _INSERT_TEMPLATE_CLONE,
        {
            "id": new_id,
            "venue_id": source["venue_id"],
            "name_he": source["name_he"],
            "name_en": source["name_en"],
            "description_he": source["description_he"],
            "description_en": source["description_en"],
            "fields": service.json_dump(source["fields"]),
            "binding_language": source["binding_language"],
            "bilingual": 1 if source["bilingual"] else 0,
            "status": "draft",
            "version": source["version"],
            "parent_id": parent_id,
            "created_by": source["created_by"],
            "created_at": service.now_iso(),
            "published_at": None,
        },
    )
    await session.commit()
    return new_id


_COUNT_RESPONSES = text("SELECT COUNT(*) AS n FROM form_responses WHERE template_id = :id")


async def delete_template(session: AsyncSession, template_id: str) -> None:
    current = await get_template(session, template_id)
    if not current:
        raise TemplateNotFound
    n = (await session.execute(_COUNT_RESPONSES, {"id": template_id})).scalar_one()
    if n and n > 0:
        raise TemplateInUse
    await session.execute(text("DELETE FROM forms_templates WHERE id = :id"), {"id": template_id})
    await session.commit()


_LIST_RESPONSES_FOR_TEMPLATE = text(
    """
    SELECT r.id, r.status, r.submitted_at, r.pdf_gcs_path, r.employee_id, r.answers,
           TRIM(COALESCE(e.name, '') || ' ' || COALESCE(e.surname, '')) AS employee_name
    FROM form_responses r
    LEFT JOIN employees e ON e.id = r.employee_id
    WHERE r.template_id = :id
    ORDER BY r.submitted_at DESC, r.created_at DESC
    """
)


async def list_responses_for_template(session: AsyncSession, template_id: str) -> list[dict]:
    rows = (
        await session.execute(_LIST_RESPONSES_FOR_TEMPLATE, {"id": template_id})
    ).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["answers"] = service.json_load(d.get("answers"), {})
        out.append(d)
    return out


# ══════════════════════════════════════════════════════════════════
# FORM RESPONSES
# ══════════════════════════════════════════════════════════════════

_RESPONSE_COLS = (
    "id, venue_id, template_id, template_version, employee_id, assignment_progress_id, "
    "answers, status, signature_base64, binding_language, binding_language_confirmed, "
    "pdf_gcs_path, pdf_sha256, submitted_at, submitted_ip, created_at, updated_at"
)

_GET_RESPONSE = text(f"SELECT {_RESPONSE_COLS} FROM form_responses WHERE id = :id")  # noqa: S608

_FIND_DRAFT = text(
    """
    SELECT id FROM form_responses
    WHERE template_id = :template_id AND employee_id = :employee_id AND status = 'draft'
      AND (
        (CAST(:apid AS TEXT) IS NULL AND assignment_progress_id IS NULL)
        OR assignment_progress_id = CAST(:apid AS TEXT)
      )
    ORDER BY created_at DESC LIMIT 1
    """
)

_GET_PUBLISHED_BY_PARENT = text(
    """
    SELECT id, venue_id, name_he, name_en, description_he, description_en, fields,
           binding_language, status, version, parent_id, created_at, published_at
    FROM forms_templates
    WHERE status = 'published'
      AND (id = :id OR parent_id = (SELECT parent_id FROM forms_templates WHERE id = :id))
    ORDER BY version DESC LIMIT 1
    """
)

_GET_LATEST_DRAFT_FOR_EMP = text(
    """
    SELECT id, answers, updated_at, assignment_progress_id
    FROM form_responses
    WHERE employee_id = :employee_id AND status = 'draft'
      AND template_id IN (
        SELECT id FROM forms_templates
        WHERE parent_id = (SELECT parent_id FROM forms_templates WHERE id = :template_id)
      )
    ORDER BY updated_at DESC, created_at DESC LIMIT 1
    """
)


def _response_row(r: Any) -> dict:
    d = dict(r)
    d["answers"] = service.json_load(d.get("answers"), {})
    return d


async def get_response(session: AsyncSession, response_id: str) -> dict | None:
    row = (await session.execute(_GET_RESPONSE, {"id": response_id})).mappings().first()
    return _response_row(row) if row else None


async def get_published_template_by_parent(session: AsyncSession, template_id: str) -> dict | None:
    row = (
        await session.execute(_GET_PUBLISHED_BY_PARENT, {"id": template_id})
    ).mappings().first()
    if not row:
        return None
    d = dict(row)
    d["fields"] = service.json_load(d.get("fields"), [])
    return d


async def get_latest_draft(session: AsyncSession, *, employee_id: int, template_id: str) -> dict | None:
    row = (
        await session.execute(
            _GET_LATEST_DRAFT_FOR_EMP,
            {"employee_id": employee_id, "template_id": template_id},
        )
    ).mappings().first()
    if not row:
        return None
    return {
        "response_id": row["id"],
        "answers": service.json_load(row["answers"], {}),
        "updated_at": row["updated_at"],
    }


async def start_response(
    session: AsyncSession,
    *,
    template_id: str,
    employee_id: int,
    assignment_progress_id: str | None,
    venue_id: str,
) -> str:
    existing = (
        await session.execute(
            _FIND_DRAFT,
            {
                "template_id": template_id,
                "employee_id": employee_id,
                "apid": assignment_progress_id,
            },
        )
    ).mappings().first()
    if existing:
        return existing["id"]

    tmpl = (
        await session.execute(
            text("SELECT version, status FROM forms_templates WHERE id = :id"),
            {"id": template_id},
        )
    ).mappings().first()
    if not tmpl or tmpl["status"] != "published":
        raise TemplateNotPublished

    new_id = service.new_id()
    now = service.now_iso()
    await session.execute(
        text(
            """
            INSERT INTO form_responses (
                id, venue_id, template_id, template_version, employee_id,
                assignment_progress_id, answers, status, created_at, updated_at
            ) VALUES (
                :id, :venue_id, :template_id, :version, :employee_id,
                :apid, '{}', 'draft', :now, :now
            )
            """
        ),
        {
            "id": new_id,
            "venue_id": venue_id,
            "template_id": template_id,
            "version": tmpl["version"],
            "employee_id": employee_id,
            "apid": assignment_progress_id,
            "now": now,
        },
    )
    await session.commit()
    return new_id


async def save_response_draft(session: AsyncSession, response_id: str, answers: dict) -> dict:
    current = await get_response(session, response_id)
    if not current:
        raise ResponseNotFound
    if current["status"] != "draft":
        raise ResponseNotDraft
    await session.execute(
        text(
            "UPDATE form_responses SET answers = :answers, updated_at = :now "
            "WHERE id = :id AND status = 'draft'"
        ),
        {"answers": service.json_dump(answers or {}), "now": service.now_iso(), "id": response_id},
    )
    await session.commit()
    updated = await get_response(session, response_id)
    assert updated is not None  # noqa: S101
    return updated


async def submit_response(
    session: AsyncSession,
    response_id: str,
    *,
    signature_base64: str | None,
    binding_language_confirmed: str | None,
    submitted_ip: str | None,
) -> dict:
    current = await get_response(session, response_id)
    if not current:
        raise ResponseNotFound
    if current["status"] != "draft":
        raise ResponseNotDraft
    tpl = await get_template(session, current["template_id"])
    binding = tpl["binding_language"] if tpl else current.get("binding_language")
    now = service.now_iso()
    await session.execute(
        text(
            """
            UPDATE form_responses
            SET status = 'submitted',
                signature_base64 = CAST(:sig AS TEXT),
                binding_language = COALESCE(CAST(:binding AS TEXT), binding_language),
                binding_language_confirmed = CAST(:blc AS TEXT),
                submitted_at = :now,
                submitted_ip = CAST(:ip AS TEXT),
                updated_at = :now
            WHERE id = :id AND status = 'draft'
            """
        ),
        {
            "sig": signature_base64,
            "binding": binding,
            "blc": binding_language_confirmed,
            "now": now,
            "ip": submitted_ip,
            "id": response_id,
        },
    )
    await session.commit()
    updated = await get_response(session, response_id)
    assert updated is not None  # noqa: S101
    return updated


async def set_pdf_for_response(
    session: AsyncSession, response_id: str, *, pdf_gcs_path: str, pdf_sha256: str
) -> dict:
    current = await get_response(session, response_id)
    if not current:
        raise ResponseNotFound
    await session.execute(
        text(
            "UPDATE form_responses SET pdf_gcs_path = :path, pdf_sha256 = :sha, "
            "updated_at = :now WHERE id = :id"
        ),
        {"path": pdf_gcs_path, "sha": pdf_sha256, "now": service.now_iso(), "id": response_id},
    )
    await session.commit()
    updated = await get_response(session, response_id)
    assert updated is not None  # noqa: S101
    return updated


_LIST_RESPONSES_BY_EMPLOYEE = text(
    """
    SELECT r.id, r.template_id, t.name_he AS template_name_he, t.name_en AS template_name_en,
           r.template_version, r.status, r.submitted_at, r.pdf_gcs_path, r.created_at
    FROM form_responses r
    LEFT JOIN forms_templates t ON t.id = r.template_id
    WHERE r.employee_id = :employee_id
      AND (CAST(:status AS TEXT) IS NULL OR r.status = CAST(:status AS TEXT))
    ORDER BY r.submitted_at DESC, r.created_at DESC
    """
)


async def list_responses_for_employee(
    session: AsyncSession, employee_id: int, status: str | None = "submitted"
) -> list[dict]:
    rows = (
        await session.execute(
            _LIST_RESPONSES_BY_EMPLOYEE, {"employee_id": employee_id, "status": status}
        )
    ).mappings().all()
    return [dict(r) for r in rows]


_GET_EMPLOYEE_BY_ID = text("SELECT id, name, surname, email, phone FROM employees WHERE id = :id")


async def get_employee_for_pdf(session: AsyncSession, employee_id: int) -> dict | None:
    row = (await session.execute(_GET_EMPLOYEE_BY_ID, {"id": employee_id})).mappings().first()
    if not row:
        return None
    emp = dict(row)
    emp["full_name"] = f"{emp.get('name') or ''} {emp.get('surname') or ''}".strip()
    return emp


# ══════════════════════════════════════════════════════════════════
# COURSES
# ══════════════════════════════════════════════════════════════════

_COURSE_COLS = (
    "id, venue_id, name_he, name_en, description_he, description_en, status, "
    "version, parent_id, created_by, created_at, published_at"
)

_LIST_COURSES = text(
    f"SELECT {_COURSE_COLS} FROM courses ORDER BY published_at DESC, created_at DESC"  # noqa: S608
)
_GET_COURSE = text(f"SELECT {_COURSE_COLS} FROM courses WHERE id = :id")  # noqa: S608

_GET_COURSE_SECTIONS = text(
    "SELECT id, course_id, position, title_he, title_en FROM course_sections "
    "WHERE course_id = :course_id ORDER BY position"
)
_GET_COURSE_ITEMS = text(
    "SELECT id, section_id, position, type, payload FROM course_items "
    "WHERE section_id IN :section_ids ORDER BY section_id, position"
).bindparams(bindparam("section_ids", expanding=True))

_UPDATABLE_COURSE_FIELDS = {"name_he", "name_en", "description_he", "description_en"}


async def list_courses(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(_LIST_COURSES)).mappings().all()
    return [dict(r) for r in rows]


async def _get_course_lean(session: AsyncSession, course_id: str) -> dict | None:
    row = (await session.execute(_GET_COURSE, {"id": course_id})).mappings().first()
    return dict(row) if row else None


async def get_course(session: AsyncSession, course_id: str) -> dict | None:
    course = await _get_course_lean(session, course_id)
    if not course:
        return None
    sections = [
        dict(s)
        for s in (
            await session.execute(_GET_COURSE_SECTIONS, {"course_id": course_id})
        ).mappings().all()
    ]
    section_ids = [s["id"] for s in sections]
    items_by_section: dict[str, list[dict]] = {sid: [] for sid in section_ids}
    if section_ids:
        item_rows = (
            await session.execute(_GET_COURSE_ITEMS, {"section_ids": section_ids})
        ).mappings().all()
        for item in item_rows:
            d = dict(item)
            d["payload"] = service.json_load(d.get("payload"), {})
            items_by_section.setdefault(item["section_id"], []).append(d)
    for s in sections:
        s["items"] = items_by_section.get(s["id"], [])
    course["sections"] = sections
    return course


async def create_course(
    session: AsyncSession,
    *,
    name_he: str,
    name_en: str,
    description_he: str | None,
    description_en: str | None,
    created_by: int | None,
    venue_id: str,
) -> str:
    new_id = service.new_id()
    await session.execute(
        text(
            """
            INSERT INTO courses (
                id, venue_id, name_he, name_en, description_he, description_en,
                created_by, status, version, parent_id, created_at
            ) VALUES (
                :id, :venue_id, :name_he, :name_en, :description_he, :description_en,
                :created_by, 'draft', 1, :id, :created_at
            )
            """
        ),
        {
            "id": new_id,
            "venue_id": venue_id,
            "name_he": name_he,
            "name_en": name_en,
            "description_he": description_he,
            "description_en": description_en,
            "created_by": created_by,
            "created_at": service.now_iso(),
        },
    )
    await session.commit()
    return new_id


async def update_course(session: AsyncSession, course_id: str, **fields_to_update) -> dict:
    current = await _get_course_lean(session, course_id)
    if not current:
        raise CourseNotFound
    if current["status"] != "draft":
        raise CourseNotDraft
    sets: list[str] = []
    params: dict[str, Any] = {"id": course_id}
    for key, value in fields_to_update.items():
        if value is None or key not in _UPDATABLE_COURSE_FIELDS:
            continue
        sets.append(f"{key} = :{key}")
        params[key] = value
    if sets:
        await session.execute(
            text(f"UPDATE courses SET {', '.join(sets)} WHERE id = :id"),  # noqa: S608
            params,
        )
        await session.commit()
    result = await get_course(session, course_id)
    assert result is not None  # noqa: S101
    return result


async def delete_course(session: AsyncSession, course_id: str) -> None:
    current = await _get_course_lean(session, course_id)
    if not current:
        raise CourseNotFound
    # Cascade in app layer (no ON DELETE CASCADE in the portable schema).
    section_rows = (
        await session.execute(
            text("SELECT id FROM course_sections WHERE course_id = :cid"), {"cid": course_id}
        )
    ).mappings().all()
    section_ids = [s["id"] for s in section_rows]
    if section_ids:
        del_items = text("DELETE FROM course_items WHERE section_id IN :sids").bindparams(
            bindparam("sids", expanding=True)
        )
        await session.execute(del_items, {"sids": section_ids})
    await session.execute(
        text("DELETE FROM course_sections WHERE course_id = :cid"), {"cid": course_id}
    )
    await session.execute(text("DELETE FROM courses WHERE id = :id"), {"id": course_id})
    await session.commit()


async def add_section(
    session: AsyncSession, course_id: str, *, title_he: str, title_en: str, position: int
) -> str:
    new_id = service.new_id()
    await session.execute(
        text(
            "INSERT INTO course_sections (id, course_id, title_he, title_en, position) "
            "VALUES (:id, :course_id, :title_he, :title_en, :position)"
        ),
        {
            "id": new_id,
            "course_id": course_id,
            "title_he": title_he,
            "title_en": title_en,
            "position": position,
        },
    )
    await session.commit()
    return new_id


async def add_item(
    session: AsyncSession, section_id: str, *, type: str, payload: dict, position: int
) -> str:
    section = (
        await session.execute(
            text("SELECT id FROM course_sections WHERE id = :id"), {"id": section_id}
        )
    ).first()
    if not section:
        raise SectionNotFound
    new_id = service.new_id()
    await session.execute(
        text(
            "INSERT INTO course_items (id, section_id, type, payload, position) "
            "VALUES (:id, :section_id, :type, :payload, :position)"
        ),
        {
            "id": new_id,
            "section_id": section_id,
            "type": type,
            "payload": service.json_dump(payload or {}),
            "position": position,
        },
    )
    await session.commit()
    return new_id


_FIND_PREV_PUBLISHED_COURSE = text(
    "SELECT id, version FROM courses WHERE parent_id = :parent AND status = 'published' "
    "ORDER BY version DESC LIMIT 1"
)
_ARCHIVE_COURSE_BY_ID = text("UPDATE courses SET status = 'archived' WHERE id = :id")


async def publish_course(session: AsyncSession, course_id: str) -> dict:
    current = await _get_course_lean(session, course_id)
    if not current:
        raise CourseNotFound
    parent_id = current["parent_id"] or current["id"]
    prev = (
        await session.execute(_FIND_PREV_PUBLISHED_COURSE, {"parent": parent_id})
    ).mappings().first()

    if prev and prev["id"] != current["id"]:
        new_version = prev["version"] + 1
        new_id = service.new_id()
        now = service.now_iso()
        await session.execute(
            text(
                """
                INSERT INTO courses (
                    id, venue_id, name_he, name_en, description_he, description_en,
                    created_by, status, version, parent_id, created_at, published_at
                ) VALUES (
                    :id, :venue_id, :name_he, :name_en, :description_he, :description_en,
                    :created_by, 'published', :version, :parent_id, :created_at, :now
                )
                """
            ),
            {
                "id": new_id,
                "venue_id": current["venue_id"],
                "name_he": current["name_he"],
                "name_en": current["name_en"],
                "description_he": current["description_he"],
                "description_en": current["description_en"],
                "created_by": current["created_by"],
                "version": new_version,
                "parent_id": parent_id,
                "created_at": now,
                "now": now,
            },
        )
        await session.execute(_ARCHIVE_COURSE_BY_ID, {"id": prev["id"]})
        await session.execute(_ARCHIVE_COURSE_BY_ID, {"id": course_id})
        await session.commit()
        result = await _get_course_lean(session, new_id)
        assert result is not None  # noqa: S101
        return result

    if current["status"] != "draft":
        raise CourseNotDraft
    now = service.now_iso()
    await session.execute(
        text("UPDATE courses SET status = 'published', published_at = :now WHERE id = :id"),
        {"now": now, "id": course_id},
    )
    await session.commit()
    result = await _get_course_lean(session, course_id)
    assert result is not None  # noqa: S101
    return result


# --- course progress --------------------------------------------------------

_GET_COURSE_PROGRESS = text(
    "SELECT id, course_id, course_version, employee_id, completed_item_ids, "
    "started_at, completed_at FROM course_progress "
    "WHERE employee_id = :employee_id AND course_id = :course_id "
    "ORDER BY started_at DESC LIMIT 1"
)

_ALL_COURSE_ITEM_IDS = text(
    """
    SELECT ci.id AS item_id
    FROM course_sections cs
    JOIN course_items ci ON ci.section_id = cs.id
    WHERE cs.course_id = :course_id
    ORDER BY cs.position, ci.position
    """
)


async def complete_course_item(
    session: AsyncSession, *, course_id: str, item_id: str, employee_id: int, venue_id: str
) -> dict:
    """Find-or-create the progress row, append *item_id* (de-duped), and flag
    whether the whole course is now complete. Returns
    ``{course_progress_id, completed_item_ids, course_complete}``."""
    existing = (
        await session.execute(
            _GET_COURSE_PROGRESS, {"employee_id": employee_id, "course_id": course_id}
        )
    ).mappings().first()

    if existing:
        completed = service.merge_completed_item(existing["completed_item_ids"], item_id)
        progress_id = existing["id"]
        await session.execute(
            text(
                "UPDATE course_progress SET last_item_id = :item_id, "
                "completed_item_ids = :cids WHERE id = :id"
            ),
            {"item_id": item_id, "cids": service.json_dump(completed), "id": progress_id},
        )
    else:
        version_row = (
            await session.execute(
                text("SELECT version FROM courses WHERE id = :id"), {"id": course_id}
            )
        ).mappings().first()
        if not version_row:
            raise CourseNotFound
        completed = [item_id]
        progress_id = service.new_id()
        await session.execute(
            text(
                """
                INSERT INTO course_progress (
                    id, venue_id, course_id, course_version, employee_id,
                    last_item_id, completed_item_ids, started_at
                ) VALUES (
                    :id, :venue_id, :course_id, :version, :employee_id,
                    :item_id, :cids, :now
                )
                """
            ),
            {
                "id": progress_id,
                "venue_id": venue_id,
                "course_id": course_id,
                "version": version_row["version"],
                "employee_id": employee_id,
                "item_id": item_id,
                "cids": service.json_dump(completed),
                "now": service.now_iso(),
            },
        )

    required = [
        r["item_id"]
        for r in (
            await session.execute(_ALL_COURSE_ITEM_IDS, {"course_id": course_id})
        ).mappings().all()
    ]
    is_complete = service.course_complete(required, completed)
    if is_complete:
        await session.execute(
            text(
                "UPDATE course_progress SET completed_at = COALESCE(completed_at, :now) "
                "WHERE id = :id"
            ),
            {"now": service.now_iso(), "id": progress_id},
        )
    await session.commit()
    return {
        "course_progress_id": progress_id,
        "completed_item_ids": completed,
        "course_complete": is_complete,
    }


# ══════════════════════════════════════════════════════════════════
# FLOWS
# ══════════════════════════════════════════════════════════════════

_FLOW_COLS = (
    "id, venue_id, name_he, name_en, ordering, default_due_days, status, created_by, created_at"
)

_LIST_FLOWS = text(
    f"""
    SELECT {_FLOW_COLS},
           (SELECT COUNT(*) FROM flow_items fi WHERE fi.flow_id = f.id) AS item_count
    FROM flows f
    WHERE (CAST(:active_only AS INTEGER) = 0 OR f.status = 'published')
    ORDER BY created_at DESC
    """  # noqa: S608
)
_GET_FLOW = text(f"SELECT {_FLOW_COLS} FROM flows WHERE id = :id")  # noqa: S608

_GET_FLOW_ITEMS = text(
    "SELECT id, flow_id, position, item_type, item_id, due_days_override "
    "FROM flow_items WHERE flow_id = :flow_id ORDER BY position"
)

_UPDATABLE_FLOW_FIELDS = {"name_he", "name_en", "ordering", "default_due_days"}


async def list_flows(session: AsyncSession, active_only: bool = False) -> list[dict]:
    rows = (
        await session.execute(_LIST_FLOWS, {"active_only": 1 if active_only else 0})
    ).mappings().all()
    return [dict(r) for r in rows]


async def _get_flow_lean(session: AsyncSession, flow_id: str) -> dict | None:
    row = (await session.execute(_GET_FLOW, {"id": flow_id})).mappings().first()
    return dict(row) if row else None


async def _flow_item_names(session: AsyncSession, items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        d = dict(it)
        table = "forms_templates" if it["item_type"] == "form" else "courses"
        name_row = (
            await session.execute(
                text(f"SELECT name_he, name_en FROM {table} WHERE id = :id"),  # noqa: S608
                {"id": it["item_id"]},
            )
        ).mappings().first()
        d["item_name_he"] = name_row["name_he"] if name_row else None
        d["item_name_en"] = name_row["name_en"] if name_row else None
        out.append(d)
    return out


async def get_flow(session: AsyncSession, flow_id: str) -> dict | None:
    flow = await _get_flow_lean(session, flow_id)
    if not flow:
        return None
    items = (
        await session.execute(_GET_FLOW_ITEMS, {"flow_id": flow_id})
    ).mappings().all()
    flow["items"] = await _flow_item_names(session, [dict(i) for i in items])
    return flow


async def create_flow(
    session: AsyncSession,
    *,
    name_he: str,
    name_en: str,
    ordering: str,
    default_due_days: int | None,
    created_by: int | None,
    venue_id: str,
) -> str:
    new_id = service.new_id()
    await session.execute(
        text(
            """
            INSERT INTO flows (
                id, venue_id, name_he, name_en, ordering, default_due_days,
                status, created_by, created_at
            ) VALUES (
                :id, :venue_id, :name_he, :name_en, :ordering, :default_due_days,
                'draft', :created_by, :created_at
            )
            """
        ),
        {
            "id": new_id,
            "venue_id": venue_id,
            "name_he": name_he,
            "name_en": name_en,
            "ordering": ordering or "sequential",
            "default_due_days": default_due_days,
            "created_by": created_by,
            "created_at": service.now_iso(),
        },
    )
    await session.commit()
    return new_id


async def update_flow(session: AsyncSession, flow_id: str, **fields_to_update) -> dict:
    current = await _get_flow_lean(session, flow_id)
    if not current:
        raise FlowNotFound
    if current["status"] != "draft":
        raise FlowNotDraft
    sets: list[str] = []
    params: dict[str, Any] = {"id": flow_id}
    for key, value in fields_to_update.items():
        if value is None or key not in _UPDATABLE_FLOW_FIELDS:
            continue
        sets.append(f"{key} = :{key}")
        params[key] = value
    if sets:
        await session.execute(
            text(f"UPDATE flows SET {', '.join(sets)} WHERE id = :id"),  # noqa: S608
            params,
        )
        await session.commit()
    result = await get_flow(session, flow_id)
    assert result is not None  # noqa: S101
    return result


async def add_flow_item(
    session: AsyncSession,
    flow_id: str,
    *,
    position: int,
    item_type: str,
    item_id: str,
    due_days_override: int | None,
) -> str:
    flow = await _get_flow_lean(session, flow_id)
    if not flow:
        raise FlowNotFound
    if flow["status"] != "draft":
        raise FlowNotDraft
    table = "forms_templates" if item_type == "form" else "courses"
    exists = (
        await session.execute(
            text(f"SELECT 1 FROM {table} WHERE id = :id"), {"id": item_id}  # noqa: S608
        )
    ).first()
    if not exists:
        raise FlowItemTargetMissing
    await session.execute(
        text("UPDATE flow_items SET position = position + 1 WHERE flow_id = :fid AND position >= :pos"),
        {"fid": flow_id, "pos": position},
    )
    new_id = service.new_id()
    await session.execute(
        text(
            "INSERT INTO flow_items (id, flow_id, position, item_type, item_id, due_days_override) "
            "VALUES (:id, :flow_id, :position, :item_type, :item_id, :override)"
        ),
        {
            "id": new_id,
            "flow_id": flow_id,
            "position": position,
            "item_type": item_type,
            "item_id": item_id,
            "override": due_days_override,
        },
    )
    await session.commit()
    return new_id


async def remove_flow_item(session: AsyncSession, item_id: str) -> None:
    row = (
        await session.execute(
            text("SELECT id FROM flow_items WHERE id = :id"), {"id": item_id}
        )
    ).first()
    if not row:
        raise FlowItemNotFound
    await session.execute(text("DELETE FROM flow_items WHERE id = :id"), {"id": item_id})
    await session.commit()


async def reorder_flow_items(session: AsyncSession, flow_id: str, item_id_order: list[str]) -> None:
    flow = await _get_flow_lean(session, flow_id)
    if not flow:
        raise FlowNotFound
    if flow["status"] != "draft":
        raise FlowNotDraft
    reposition = text("UPDATE flow_items SET position = :pos WHERE id = :id AND flow_id = :fid")
    # Two-phase to dodge any position collision mid-update.
    for i, iid in enumerate(item_id_order):
        await session.execute(reposition, {"pos": 10_000 + i, "id": iid, "fid": flow_id})
    for i, iid in enumerate(item_id_order):
        await session.execute(reposition, {"pos": i, "id": iid, "fid": flow_id})
    await session.commit()


async def publish_flow(session: AsyncSession, flow_id: str) -> dict:
    current = await _get_flow_lean(session, flow_id)
    if not current:
        raise FlowNotFound
    if current["status"] != "draft":
        raise FlowNotDraft
    await session.execute(
        text("UPDATE flows SET status = 'published' WHERE id = :id"), {"id": flow_id}
    )
    await session.commit()
    result = await _get_flow_lean(session, flow_id)
    assert result is not None  # noqa: S101
    return result


# ══════════════════════════════════════════════════════════════════
# ASSIGNMENTS + assignment_progress
# ══════════════════════════════════════════════════════════════════

_ASSIGNMENT_BASE = text(
    """
    SELECT a.id, a.venue_id, a.employee_id,
           TRIM(COALESCE(e.name, '') || ' ' || COALESCE(e.surname, '')) AS employee_name,
           a.source, a.flow_id, a.form_id, a.course_id, a.assigned_by,
           a.assigned_at, a.due_at, a.last_nudged_at,
           COALESCE(f.name_he, ft.name_he, c.name_he) AS target_name_he,
           COALESCE(f.name_en, ft.name_en, c.name_en) AS target_name_en,
           f.ordering AS flow_ordering, f.default_due_days AS flow_default_due_days
    FROM assignments a
    LEFT JOIN employees e ON e.id = a.employee_id
    LEFT JOIN flows f ON f.id = a.flow_id
    LEFT JOIN forms_templates ft ON ft.id = a.form_id
    LEFT JOIN courses c ON c.id = a.course_id
    WHERE a.id = :id
    """
)

_LIST_ASSIGNMENTS = text(
    """
    SELECT a.id, a.venue_id, a.employee_id,
           TRIM(COALESCE(e.name, '') || ' ' || COALESCE(e.surname, '')) AS employee_name,
           a.source, a.flow_id, a.form_id, a.course_id, a.assigned_by,
           a.assigned_at, a.due_at, a.last_nudged_at,
           COALESCE(f.name_he, ft.name_he, c.name_he) AS target_name_he,
           COALESCE(f.name_en, ft.name_en, c.name_en) AS target_name_en,
           f.ordering AS flow_ordering
    FROM assignments a
    LEFT JOIN employees e ON e.id = a.employee_id
    LEFT JOIN flows f ON f.id = a.flow_id
    LEFT JOIN forms_templates ft ON ft.id = a.form_id
    LEFT JOIN courses c ON c.id = a.course_id
    WHERE (CAST(:employee_id AS INTEGER) IS NULL OR a.employee_id = CAST(:employee_id AS INTEGER))
    """
)

_PROGRESS_FOR_ASSIGNMENTS = text(
    "SELECT id, assignment_id, flow_item_id, form_response_id, course_progress_id, "
    "status, due_at, started_at, completed_at FROM assignment_progress "
    "WHERE assignment_id IN :aids"
).bindparams(bindparam("aids", expanding=True))


async def create_assignment(
    session: AsyncSession,
    *,
    employee_id: int,
    source: str,
    flow_id: str | None = None,
    form_id: str | None = None,
    course_id: str | None = None,
    assigned_by: int | None,
    due_at: str | None = None,
    venue_id: str,
) -> str:
    if source == "flow":
        if not flow_id:
            raise AssignmentSourceInvalid("flow_id required for source='flow'")
    elif source == "standalone":
        if not (form_id or course_id) or (form_id and course_id):
            raise AssignmentSourceInvalid(
                "exactly one of form_id/course_id required for source='standalone'"
            )
    else:
        raise AssignmentSourceInvalid(f"unknown source '{source}'")

    assignment_id = service.new_id()
    assigned_at = service.now_iso()
    await session.execute(
        text(
            """
            INSERT INTO assignments (
                id, venue_id, employee_id, source, flow_id, form_id, course_id,
                assigned_by, assigned_at, due_at
            ) VALUES (
                :id, :venue_id, :employee_id, :source, :flow_id, :form_id, :course_id,
                :assigned_by, :assigned_at, :due_at
            )
            """
        ),
        {
            "id": assignment_id,
            "venue_id": venue_id,
            "employee_id": employee_id,
            "source": source,
            "flow_id": flow_id,
            "form_id": form_id,
            "course_id": course_id,
            "assigned_by": assigned_by,
            "assigned_at": assigned_at,
            "due_at": due_at,
        },
    )

    if source == "flow":
        flow = await _get_flow_lean(session, flow_id)  # type: ignore[arg-type]
        if not flow:
            raise AssignmentSourceInvalid("flow not found")
        items = (
            await session.execute(_GET_FLOW_ITEMS, {"flow_id": flow_id})
        ).mappings().all()
        ordering = flow["ordering"]
        default_due_days = flow["default_due_days"]
        for i, item in enumerate(items):
            status = service.initial_progress_status(ordering, i)
            item_due_at = service.compute_due_at(
                assigned_at, default_due_days, item["due_days_override"]
            )
            await _insert_progress(
                session,
                assignment_id=assignment_id,
                flow_item_id=item["id"],
                status=status,
                due_at=item_due_at,
            )
    else:
        await _insert_progress(
            session,
            assignment_id=assignment_id,
            flow_item_id=None,
            status="available",
            due_at=due_at,
        )

    await session.commit()
    return assignment_id


async def _insert_progress(
    session: AsyncSession,
    *,
    assignment_id: str,
    flow_item_id: str | None,
    status: str,
    due_at: str | None,
) -> str:
    new_id = service.new_id()
    await session.execute(
        text(
            "INSERT INTO assignment_progress (id, assignment_id, flow_item_id, status, due_at) "
            "VALUES (:id, :aid, :fiid, :status, :due_at)"
        ),
        {"id": new_id, "aid": assignment_id, "fiid": flow_item_id, "status": status, "due_at": due_at},
    )
    return new_id


async def list_assignments(
    session: AsyncSession,
    employee_id: int | None = None,
    status_filter: str | None = None,
    sort: str = "due_at_asc",
) -> list[dict]:
    rows = (
        await session.execute(_LIST_ASSIGNMENTS, {"employee_id": employee_id})
    ).mappings().all()
    assignments = [dict(r) for r in rows]
    if not assignments:
        return []

    aids = [a["id"] for a in assignments]
    progress_rows = (
        await session.execute(_PROGRESS_FOR_ASSIGNMENTS, {"aids": aids})
    ).mappings().all()
    by_assignment: dict[str, list[dict]] = {aid: [] for aid in aids}
    for p in progress_rows:
        by_assignment.setdefault(p["assignment_id"], []).append(dict(p))

    out = []
    for a in assignments:
        rollup = service.rollup_progress(by_assignment.get(a["id"], []))
        a.update(rollup)
        if service.matches_status_filter(rollup, status_filter):
            out.append(a)
    return service.sort_assignments(out, sort)


async def get_assignment(session: AsyncSession, assignment_id: str) -> dict | None:
    row = (await session.execute(_ASSIGNMENT_BASE, {"id": assignment_id})).mappings().first()
    if not row:
        return None
    assignment = dict(row)

    progress_rows = (
        await session.execute(
            text(
                "SELECT id, flow_item_id, form_response_id, course_progress_id, status, "
                "due_at, started_at, completed_at FROM assignment_progress "
                "WHERE assignment_id = :aid"
            ),
            {"aid": assignment_id},
        )
    ).mappings().all()

    items: list[dict] = []
    completed = 0
    for pr in progress_rows:
        item = await _resolve_progress_item(session, dict(pr), assignment)
        if item["status"] == "completed":
            completed += 1
        items.append(item)
    items.sort(key=lambda i: i["position"])

    assignment["items"] = items
    assignment["total_items"] = len(items)
    assignment["completed_items"] = completed
    return assignment


async def _resolve_progress_item(session: AsyncSession, pr: dict, assignment: dict) -> dict:
    """Resolve one progress row to a nested item dict (item identity, name, PDF path)."""
    if pr["flow_item_id"]:
        fi = (
            await session.execute(
                text(
                    "SELECT position, item_type, item_id FROM flow_items WHERE id = :id"
                ),
                {"id": pr["flow_item_id"]},
            )
        ).mappings().first()
        position = fi["position"] if fi else 0
        item_type = fi["item_type"] if fi else None
        item_id = fi["item_id"] if fi else None
    else:
        position = 0
        if assignment["form_id"]:
            item_type, item_id = "form", assignment["form_id"]
        elif assignment["course_id"]:
            item_type, item_id = "course", assignment["course_id"]
        else:
            item_type, item_id = None, None

    item_name_he = item_name_en = None
    if item_type == "form" and item_id:
        nr = (
            await session.execute(
                text("SELECT name_he, name_en FROM forms_templates WHERE id = :id"),
                {"id": item_id},
            )
        ).mappings().first()
        if nr:
            item_name_he, item_name_en = nr["name_he"], nr["name_en"]
    elif item_type == "course" and item_id:
        nr = (
            await session.execute(
                text("SELECT name_he, name_en FROM courses WHERE id = :id"), {"id": item_id}
            )
        ).mappings().first()
        if nr:
            item_name_he, item_name_en = nr["name_he"], nr["name_en"]

    # Forward link (progress.form_response_id) OR reverse link
    # (form_responses.assignment_progress_id) — the filler sets the latter.
    form_response_id = pr["form_response_id"]
    pdf_gcs_path = None
    if form_response_id:
        fr = (
            await session.execute(
                text("SELECT pdf_gcs_path FROM form_responses WHERE id = :id"),
                {"id": form_response_id},
            )
        ).mappings().first()
        pdf_gcs_path = fr["pdf_gcs_path"] if fr else None
    else:
        rev = (
            await session.execute(
                text(
                    "SELECT id, pdf_gcs_path FROM form_responses "
                    "WHERE assignment_progress_id = :pid AND status = 'submitted' "
                    "ORDER BY submitted_at DESC LIMIT 1"
                ),
                {"pid": pr["id"]},
            )
        ).mappings().first()
        if rev:
            form_response_id = rev["id"]
            pdf_gcs_path = rev["pdf_gcs_path"]

    return {
        "progress_id": pr["id"],
        "position": position,
        "item_type": item_type,
        "item_id": item_id,
        "form_response_id": form_response_id,
        "course_progress_id": pr["course_progress_id"],
        "status": pr["status"],
        "due_at": pr["due_at"],
        "started_at": pr["started_at"],
        "completed_at": pr["completed_at"],
        "item_name_he": item_name_he,
        "item_name_en": item_name_en,
        "pdf_gcs_path": pdf_gcs_path,
    }


async def nudge_assignment(session: AsyncSession, assignment_id: str) -> dict:
    row = (
        await session.execute(
            text("SELECT id FROM assignments WHERE id = :id"), {"id": assignment_id}
        )
    ).first()
    if not row:
        raise AssignmentNotFound
    now = service.now_iso()
    await session.execute(
        text("UPDATE assignments SET last_nudged_at = :now WHERE id = :id"),
        {"now": now, "id": assignment_id},
    )
    await session.commit()
    return {"id": assignment_id, "last_nudged_at": now}


async def delete_assignment(session: AsyncSession, assignment_id: str) -> bool:
    row = (
        await session.execute(
            text("SELECT id FROM assignments WHERE id = :id"), {"id": assignment_id}
        )
    ).first()
    if not row:
        return False
    # Cascade progress rows in the app layer; submitted responses are kept.
    await session.execute(
        text("DELETE FROM assignment_progress WHERE assignment_id = :aid"),
        {"aid": assignment_id},
    )
    await session.execute(text("DELETE FROM assignments WHERE id = :id"), {"id": assignment_id})
    await session.commit()
    return True


_GET_PROGRESS = text(
    "SELECT id, assignment_id, flow_item_id, status, due_at, started_at, completed_at "
    "FROM assignment_progress WHERE id = :id"
)


async def update_progress_status(
    session: AsyncSession,
    progress_id: str,
    new_status: str,
    completed_at: str | None = None,
) -> dict:
    current = (
        await session.execute(_GET_PROGRESS, {"id": progress_id})
    ).mappings().first()
    if not current:
        raise ProgressNotFound
    updated = await _apply_progress_status(session, dict(current), new_status, completed_at)

    # Sequential unlock: completing a flow item flips the next locked item available.
    if new_status == "completed" and updated["flow_item_id"]:
        ordering_row = (
            await session.execute(
                text(
                    "SELECT a.source, f.ordering FROM assignments a "
                    "LEFT JOIN flows f ON f.id = a.flow_id WHERE a.id = :id"
                ),
                {"id": updated["assignment_id"]},
            )
        ).mappings().first()
        if ordering_row and ordering_row["ordering"] == "sequential":
            fi = (
                await session.execute(
                    text("SELECT position FROM flow_items WHERE id = :id"),
                    {"id": updated["flow_item_id"]},
                )
            ).mappings().first()
            if fi is not None:
                next_row = (
                    await session.execute(
                        text(
                            """
                            SELECT ap.id FROM assignment_progress ap
                            JOIN flow_items fi_next ON fi_next.id = ap.flow_item_id
                            WHERE ap.assignment_id = :aid AND ap.status = 'locked'
                              AND fi_next.position = :next_pos
                            ORDER BY fi_next.position LIMIT 1
                            """
                        ),
                        {"aid": updated["assignment_id"], "next_pos": fi["position"] + 1},
                    )
                ).mappings().first()
                if next_row:
                    nxt = (
                        await session.execute(_GET_PROGRESS, {"id": next_row["id"]})
                    ).mappings().first()
                    if nxt is not None:
                        await _apply_progress_status(session, dict(nxt), "available", None)

    await session.commit()
    return updated


async def _apply_progress_status(
    session: AsyncSession, current: dict, new_status: str, completed_at: str | None
) -> dict:
    now = service.now_iso()
    new_completed_at = current["completed_at"]
    if new_status == "completed":
        new_completed_at = completed_at or current["completed_at"] or now
    new_started_at = current["started_at"]
    if new_status in ("in_progress", "completed") and not new_started_at:
        new_started_at = now
    await session.execute(
        text(
            "UPDATE assignment_progress SET status = :status, completed_at = :cat, "
            "started_at = :sat WHERE id = :id"
        ),
        {"status": new_status, "cat": new_completed_at, "sat": new_started_at, "id": current["id"]},
    )
    return {
        "id": current["id"],
        "assignment_id": current["assignment_id"],
        "flow_item_id": current["flow_item_id"],
        "status": new_status,
        "due_at": current["due_at"],
        "started_at": new_started_at,
        "completed_at": new_completed_at,
    }


__all__ = [name for name in dir() if not name.startswith("_")]
