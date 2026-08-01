"""Tasks & checklists CRUD — ``text()`` SQL constants + async functions.

All SQL is dialect-agnostic (sqlite in tests, postgres in prod), mirroring the
hr/scheduling domains:

* unqualified table names; surrogate ids allocated app-side (``_next_id``);
* booleans stored as INTEGER 0/1; times/dates/timestamps as TEXT; JSON as TEXT
  (parsed/dumped via :mod:`be.app.domains.tasks.service`);
* upserts / membership replaces are select-then-insert (no ``ON CONFLICT``);
* id lists use an expanding IN bind (no postgres-only ``ANY(...)``);
* the 2-year prune does its date math in Python (no ``INTERVAL``);
* window-overlap / recurrence / audience unions are computed in Python.

OWNERSHIP: the router resolves the acting employee from the VERIFIED token and
passes that ``employee_id`` in. The staff guards here (:func:`_load_task_checked`,
:func:`get_staff_run`) additionally enforce run-assignee membership + task
visibility so one member can never read or act on another's run/task.

BONUS crediting is DEFERRED to the payroll domain — see the hook in
:func:`finish_task`. ``is_bonus``/``bonus_amount`` and the run-execution "bonus
owned" semantics stay here; no bonus ledger table is written in this increment.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from be.app.domains.tasks import service

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _num_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _float_or_none(value: Any) -> float | None:
    return float(value) if value is not None else None


async def _next_id(session: AsyncSession, table: str) -> int:
    stmt = text(f"SELECT COALESCE(MAX(id), 0) + 1 AS n FROM {table}")  # noqa: S608 - fixed literal
    row = (await session.execute(stmt)).mappings().first()
    return int(row["n"]) if row is not None else 1


# --------------------------------------------------------------------------- #
# Identity / clock-in gating
# --------------------------------------------------------------------------- #

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

_IS_CLOCKED_IN = text(
    "SELECT 1 FROM shifts WHERE employee_id = :eid AND shift_date = :d "
    "AND end_time IS NULL LIMIT 1"
)


async def get_admin_id(
    session: AsyncSession, *, uid: str | None, email: str | None
) -> int | None:
    row = (await session.execute(_ADMIN_ID, {"uid": uid, "email": email})).mappings().first()
    return int(row["id"]) if row is not None else None


async def is_clocked_in(session: AsyncSession, *, employee_id: int, d: str) -> bool:
    """Soft dependency on the scheduling ``shifts`` table (R18c): an open shift
    (``end_time IS NULL``) on day *d* means the caller is clocked in."""
    row = (
        await session.execute(_IS_CLOCKED_IN, {"eid": employee_id, "d": d})
    ).first()
    return row is not None


# --------------------------------------------------------------------------- #
# Task templates (library)
# --------------------------------------------------------------------------- #

_LIST_TEMPLATES = text(
    """
    SELECT id, title, description, category, estimated_minutes, sort_order,
           is_active, is_bonus, bonus_amount, bonus_currency, recurrence,
           photos, default_proof_type, required_staff, subtasks
    FROM task_templates
    WHERE (CAST(:q AS TEXT) IS NULL
           OR lower(title) LIKE :qlike
           OR lower(COALESCE(description, '')) LIKE :qlike)
      AND (CAST(:cat AS TEXT) IS NULL OR category = CAST(:cat AS TEXT))
    ORDER BY sort_order, title
    """
)

_ALL_DEPS = text("SELECT task_template_id, depends_on_id FROM task_dependencies")

_TEMPLATE_USAGE = text(
    "SELECT ci.task_template_id AS tid, c.id AS cid, c.name AS name "
    "FROM checklist_items ci JOIN checklists c ON c.id = ci.checklist_id"
)


async def list_templates(
    session: AsyncSession, *, q: str | None, category: str | None
) -> list[dict]:
    qlike = f"%{q.lower()}%" if q else None
    rows = (
        await session.execute(
            _LIST_TEMPLATES, {"q": q or None, "qlike": qlike, "cat": category or None}
        )
    ).mappings().all()

    deps_by_tpl: dict[int, list[int]] = {}
    for d in (await session.execute(_ALL_DEPS)).mappings().all():
        deps_by_tpl.setdefault(d["task_template_id"], []).append(d["depends_on_id"])

    used_by_tpl: dict[int, list[dict]] = {}
    for u in (await session.execute(_TEMPLATE_USAGE)).mappings().all():
        used_by_tpl.setdefault(u["tid"], []).append({"id": u["cid"], "name": u["name"]})

    out: list[dict] = []
    for r in rows:
        used = used_by_tpl.get(r["id"], [])
        out.append(
            {
                "id": r["id"],
                "title": r["title"],
                "description": r["description"],
                "category": r["category"],
                "estimated_minutes": r["estimated_minutes"],
                "sort_order": r["sort_order"],
                "is_active": bool(r["is_active"]),
                "is_bonus": bool(r["is_bonus"]),
                "bonus_amount": _float_or_none(r["bonus_amount"]),
                "bonus_currency": r["bonus_currency"],
                "recurrence": r["recurrence"],
                "photos": service.json_load(r["photos"], []),
                "default_proof_type": r["default_proof_type"],
                "required_staff": r["required_staff"],
                "subtasks": service.json_load(r["subtasks"], []),
                "dependencies": sorted(deps_by_tpl.get(r["id"], [])),
                "used_in": len(used),
                "used_in_checklists": sorted(used, key=lambda c: c["name"]),
            }
        )
    return out


_INSERT_TEMPLATE = text(
    """
    INSERT INTO task_templates (
        id, title, description, category, estimated_minutes, sort_order,
        is_active, is_bonus, bonus_amount, bonus_currency, recurrence,
        photos, default_proof_type, required_staff, subtasks, created_at
    ) VALUES (
        :id, :title, :desc, :cat, :mins, :sort,
        1, :bonus, CAST(:amt AS NUMERIC), :cur, :rec,
        :photos, :proof, :staff, :subtasks, :created_at
    )
    """
)


async def create_template(session: AsyncSession, *, fields: dict) -> dict:
    new_id = await _next_id(session, "task_templates")
    await session.execute(
        _INSERT_TEMPLATE,
        {
            "id": new_id,
            "title": fields["title"],
            "desc": fields.get("description"),
            "cat": fields["category"],
            "mins": fields.get("estimated_minutes"),
            "sort": fields.get("sort_order", 0),
            "bonus": 1 if fields.get("is_bonus") else 0,
            "amt": _num_str(fields.get("bonus_amount")),
            "cur": fields.get("bonus_currency", "ILS"),
            "rec": fields.get("recurrence"),
            "photos": service.json_dump(fields.get("photos") or []),
            "proof": fields.get("default_proof_type", "check"),
            "staff": fields.get("required_staff", 1),
            "subtasks": service.json_dump(fields.get("subtasks") or []),
            "created_at": _now_iso(),
        },
    )
    await session.commit()
    return {"id": new_id}


_GET_TEMPLATE = text("SELECT id, is_bonus, photos FROM task_templates WHERE id = :tid")


async def get_template(session: AsyncSession, *, template_id: int) -> dict | None:
    row = (await session.execute(_GET_TEMPLATE, {"tid": template_id})).mappings().first()
    return dict(row) if row is not None else None


# column -> (sql fragment, param name, transform)
_TEMPLATE_UPDATABLE: dict[str, tuple[str, str, Any]] = {
    "title": ("title = :title", "title", None),
    "description": ("description = :description", "description", None),
    "category": ("category = :category", "category", None),
    "estimated_minutes": ("estimated_minutes = :estimated_minutes", "estimated_minutes", None),
    "sort_order": ("sort_order = :sort_order", "sort_order", None),
    "is_active": ("is_active = :is_active", "is_active", "bool"),
    "is_bonus": ("is_bonus = :is_bonus", "is_bonus", "bool"),
    "bonus_amount": ("bonus_amount = CAST(:bonus_amount AS NUMERIC)", "bonus_amount", "num"),
    "bonus_currency": ("bonus_currency = :bonus_currency", "bonus_currency", None),
    "recurrence": ("recurrence = :recurrence", "recurrence", None),
    "photos": ("photos = :photos", "photos", "json"),
    "default_proof_type": ("default_proof_type = :default_proof_type", "default_proof_type", None),
    "required_staff": ("required_staff = :required_staff", "required_staff", None),
    "subtasks": ("subtasks = :subtasks", "subtasks", "json"),
}


async def update_template(
    session: AsyncSession, *, template_id: int, fields: dict
) -> bool:
    sets: list[str] = []
    params: dict[str, Any] = {"tid": template_id}
    for key, value in fields.items():
        spec = _TEMPLATE_UPDATABLE.get(key)
        if spec is None:
            continue
        frag, pname, transform = spec
        sets.append(frag)
        if transform == "bool":
            params[pname] = 1 if value else 0
        elif transform == "num":
            params[pname] = _num_str(value)
        elif transform == "json":
            params[pname] = service.json_dump(value)
        else:
            params[pname] = value
    if not sets:
        return False
    result = await session.execute(
        text(f"UPDATE task_templates SET {', '.join(sets)} WHERE id = :tid"),  # noqa: S608 - fixed fragments
        params,
    )
    await session.commit()
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def set_template_photos(
    session: AsyncSession, *, template_id: int, photos: list[str]
) -> None:
    await session.execute(
        text("UPDATE task_templates SET photos = :p WHERE id = :tid"),
        {"p": service.json_dump(photos), "tid": template_id},
    )
    await session.commit()


# --- dependencies -----------------------------------------------------------

_DEP_EXISTS = text(
    "SELECT 1 FROM task_dependencies WHERE task_template_id = :tid AND depends_on_id = :did"
)
_INSERT_DEP = text(
    "INSERT INTO task_dependencies (task_template_id, depends_on_id) VALUES (:tid, :did)"
)
_DELETE_DEP = text(
    "DELETE FROM task_dependencies WHERE task_template_id = :tid AND depends_on_id = :did"
)


async def add_dependency(session: AsyncSession, *, template_id: int, depends_on_id: int) -> None:
    exists = (
        await session.execute(_DEP_EXISTS, {"tid": template_id, "did": depends_on_id})
    ).first()
    if not exists:
        await session.execute(_INSERT_DEP, {"tid": template_id, "did": depends_on_id})
    await session.commit()


async def remove_dependency(session: AsyncSession, *, template_id: int, depends_on_id: int) -> None:
    await session.execute(_DELETE_DEP, {"tid": template_id, "did": depends_on_id})
    await session.commit()


# --------------------------------------------------------------------------- #
# Checklists
# --------------------------------------------------------------------------- #

_LIST_CHECKLISTS = text(
    """
    SELECT c.id, c.name, c.group_id, g.name AS group_name, g.color AS group_color,
           c.category, c.is_active, c.due_time, c.recurrence, c.auto_generate,
           c.approval_status, c.approval_note, c.approved_by, c.approved_at,
           c.created_by, c.updated_at,
           (SELECT COUNT(*) FROM checklist_items ci WHERE ci.checklist_id = c.id) AS item_count,
           (SELECT COUNT(*) FROM checklist_assignees ca WHERE ca.checklist_id = c.id) AS assignee_count
    FROM checklists c
    LEFT JOIN employee_groups g ON g.id = c.group_id
    WHERE c.is_adhoc = 0
      AND (CAST(:ast AS TEXT) IS NULL OR c.approval_status = CAST(:ast AS TEXT))
    ORDER BY c.name
    """
)


def _checklist_row(r: Any) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "group_id": r["group_id"],
        "group_name": r["group_name"],
        "group_color": r["group_color"],
        "category": r["category"],
        "is_active": bool(r["is_active"]),
        "due_time": r["due_time"],
        "recurrence": service.json_load(r["recurrence"], None),
        "auto_generate": bool(r["auto_generate"]),
        "approval_status": r["approval_status"],
        "approval_note": r["approval_note"],
        "approved_by": r["approved_by"],
        "approved_at": r["approved_at"],
        "created_by": r["created_by"],
        "updated_at": r["updated_at"],
    }


async def list_checklists(
    session: AsyncSession, *, approval_status: str | None
) -> list[dict]:
    rows = (
        await session.execute(_LIST_CHECKLISTS, {"ast": approval_status or None})
    ).mappings().all()
    out = []
    for r in rows:
        item = _checklist_row(r)
        item["item_count"] = r["item_count"]
        item["assignee_count"] = r["assignee_count"]
        out.append(item)
    return out


_GET_CHECKLIST_FULL = text(
    """
    SELECT c.id, c.name, c.group_id, g.name AS group_name, g.color AS group_color,
           c.category, c.is_active, c.due_time, c.recurrence, c.auto_generate,
           c.approval_status, c.approval_note, c.approved_by, c.approved_at,
           c.created_by, c.updated_at, c.is_adhoc
    FROM checklists c LEFT JOIN employee_groups g ON g.id = c.group_id
    WHERE c.id = :cid
    """
)

# Lean row used by generation / run reads.
_GET_CHECKLIST = text(
    "SELECT id, name, group_id, category, is_active, due_time, recurrence, "
    "auto_generate, approval_status FROM checklists WHERE id = :cid"
)


async def get_checklist(session: AsyncSession, *, checklist_id: int) -> dict | None:
    r = (await session.execute(_GET_CHECKLIST_FULL, {"cid": checklist_id})).mappings().first()
    if r is None:
        return None
    out = _checklist_row(r)
    out["is_adhoc"] = bool(r["is_adhoc"])
    return out


async def get_checklist_lean(session: AsyncSession, *, checklist_id: int) -> dict | None:
    r = (await session.execute(_GET_CHECKLIST, {"cid": checklist_id})).mappings().first()
    return dict(r) if r is not None else None


_INSERT_CHECKLIST = text(
    """
    INSERT INTO checklists (
        id, name, group_id, category, is_active, due_time, recurrence,
        auto_generate, created_by, updated_at, approval_status, is_adhoc, created_at
    ) VALUES (
        :id, :name, :gid, :cat, 1, :due, :rec,
        :auto, :by, :updated_at, :ast, 0, :created_at
    )
    """
)


async def create_checklist(
    session: AsyncSession, *, fields: dict, created_by: str | None, approval_status: str
) -> dict:
    new_id = await _next_id(session, "checklists")
    now = _now_iso()
    await session.execute(
        _INSERT_CHECKLIST,
        {
            "id": new_id,
            "name": fields["name"],
            "gid": fields.get("group_id"),
            "cat": fields["category"],
            "due": fields.get("due_time"),
            "rec": service.json_dump(fields["recurrence"]) if fields.get("recurrence") is not None else None,
            "auto": 1 if fields.get("auto_generate") else 0,
            "by": created_by,
            "updated_at": now,
            "ast": approval_status,
            "created_at": now,
        },
    )
    await session.commit()
    return {"id": new_id, "approval_status": approval_status, "updated_at": now}


async def stale_check(
    session: AsyncSession, *, checklist_id: int, client_updated_at: str | None
) -> dict:
    """R11 concurrent-edit guard. Returns the current row (404 if missing, 409 if
    the caller's ``updated_at`` no longer matches the stored one)."""
    row = (
        await session.execute(
            text("SELECT id, updated_at, approval_status FROM checklists WHERE id = :cid"),
            {"cid": checklist_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="checklist not found")
    if client_updated_at and row["updated_at"] and client_updated_at != row["updated_at"]:
        raise HTTPException(
            status_code=409,
            detail="stale: this checklist changed since you opened it — reload to continue",
        )
    return dict(row)


async def _touch_checklist(
    session: AsyncSession, *, checklist_id: int, is_full_admin: bool
) -> dict:
    """Manager writes -> pending_approval (pausing future runs); admin writes keep
    approved (R36). Bumps updated_at (R11)."""
    status = "approved" if is_full_admin else "pending_approval"
    now = _now_iso()
    await session.execute(
        text(
            "UPDATE checklists SET approval_status = :st, updated_at = :now WHERE id = :cid"
        ),
        {"st": status, "now": now, "cid": checklist_id},
    )
    return {"approval_status": status, "updated_at": now}


_CHECKLIST_UPDATABLE: dict[str, tuple[str, str, Any]] = {
    "name": ("name = :name", "name", None),
    "group_id": ("group_id = :group_id", "group_id", None),
    "category": ("category = :category", "category", None),
    "is_active": ("is_active = :is_active", "is_active", "bool"),
    "due_time": ("due_time = :due_time", "due_time", None),
    "recurrence": ("recurrence = :recurrence", "recurrence", "json"),
    "auto_generate": ("auto_generate = :auto_generate", "auto_generate", "bool"),
}


async def update_checklist(
    session: AsyncSession, *, checklist_id: int, fields: dict, is_full_admin: bool
) -> dict:
    sets: list[str] = []
    params: dict[str, Any] = {"cid": checklist_id}
    for key, value in fields.items():
        spec = _CHECKLIST_UPDATABLE.get(key)
        if spec is None:
            continue
        frag, pname, transform = spec
        sets.append(frag)
        if transform == "bool":
            params[pname] = 1 if value else 0
        elif transform == "json":
            params[pname] = service.json_dump(value)
        else:
            params[pname] = value
    if fields.get("is_active"):
        sets.append("activated_at = COALESCE(activated_at, :activated_at)")
        params["activated_at"] = _now_iso()
    if sets:
        await session.execute(
            text(f"UPDATE checklists SET {', '.join(sets)} WHERE id = :cid"),  # noqa: S608 - fixed fragments
            params,
        )
    touched = await _touch_checklist(session, checklist_id=checklist_id, is_full_admin=is_full_admin)
    await session.commit()
    return touched


_APPROVE_CHECKLIST = text(
    """
    UPDATE checklists
    SET approval_status = 'approved', approved_by = :by, approved_at = :now,
        approval_note = NULL, is_active = 1,
        activated_at = COALESCE(activated_at, :now), updated_at = :now
    WHERE id = :cid
    """
)

_REJECT_CHECKLIST = text(
    """
    UPDATE checklists
    SET approval_status = 'rejected', approval_note = :note, approved_by = :by,
        approved_at = :now, is_active = 0, updated_at = :now
    WHERE id = :cid
    """
)


async def approve_checklist(session: AsyncSession, *, checklist_id: int, by: str | None) -> bool:
    cl = await get_checklist_lean(session, checklist_id=checklist_id)
    if cl is None:
        return False
    await session.execute(_APPROVE_CHECKLIST, {"by": by, "now": _now_iso(), "cid": checklist_id})
    await session.commit()
    return True


async def reject_checklist(
    session: AsyncSession, *, checklist_id: int, note: str, by: str | None
) -> bool:
    cl = await get_checklist_lean(session, checklist_id=checklist_id)
    if cl is None:
        return False
    await session.execute(
        _REJECT_CHECKLIST, {"note": note, "by": by, "now": _now_iso(), "cid": checklist_id}
    )
    await session.commit()
    return True


# --- checklist items --------------------------------------------------------

_GET_ITEMS = text(
    """
    SELECT ci.id, ci.task_template_id, t.title, t.description, t.photos, t.subtasks,
           t.category, t.estimated_minutes, t.is_bonus, t.bonus_amount, t.bonus_currency,
           t.default_proof_type AS template_proof_type,
           t.required_staff AS template_required_staff, t.is_active AS template_is_active,
           ci.sort_order, ci.is_mandatory, ci.proof_type, ci.required_staff,
           ci.assigned_employee_ids, ci.assigned_group_ids, ci.assigned_role_ids
    FROM checklist_items ci
    JOIN task_templates t ON t.id = ci.task_template_id
    WHERE ci.checklist_id = :cid ORDER BY ci.sort_order
    """
)


async def get_checklist_items(session: AsyncSession, *, checklist_id: int) -> list[dict]:
    rows = (await session.execute(_GET_ITEMS, {"cid": checklist_id})).mappings().all()
    return [
        {
            "id": r["id"],
            "task_template_id": r["task_template_id"],
            "title": r["title"],
            "description": r["description"],
            "photos": service.json_load(r["photos"], []),
            "subtasks": service.json_load(r["subtasks"], []),
            "category": r["category"],
            "estimated_minutes": r["estimated_minutes"],
            "is_bonus": bool(r["is_bonus"]),
            "bonus_amount": _float_or_none(r["bonus_amount"]),
            "bonus_currency": r["bonus_currency"],
            "template_is_active": bool(r["template_is_active"]),
            "sort_order": r["sort_order"],
            "is_mandatory": bool(r["is_mandatory"]),
            "proof_type": r["proof_type"],
            "required_staff": r["required_staff"],
            "effective_proof_type": r["proof_type"] or r["template_proof_type"],
            "effective_required_staff": r["required_staff"] or r["template_required_staff"],
            "assigned_employee_ids": service.json_load(r["assigned_employee_ids"], []),
            "assigned_group_ids": service.json_load(r["assigned_group_ids"], []),
            "assigned_role_ids": service.json_load(r["assigned_role_ids"], []),
        }
        for r in rows
    ]


_INSERT_ITEM = text(
    """
    INSERT INTO checklist_items (
        id, checklist_id, task_template_id, sort_order, is_mandatory, proof_type,
        required_staff, assigned_employee_ids, assigned_group_ids, assigned_role_ids
    ) VALUES (
        :id, :cid, :tid, :sort, :mand, :proof,
        :staff, :emps, :grps, :roles
    )
    """
)


async def set_checklist_items(
    session: AsyncSession, *, checklist_id: int, items: list[dict], is_full_admin: bool
) -> dict:
    await session.execute(
        text("DELETE FROM checklist_items WHERE checklist_id = :cid"), {"cid": checklist_id}
    )
    next_id = await _next_id(session, "checklist_items")
    for idx, it in enumerate(items):
        await session.execute(
            _INSERT_ITEM,
            {
                "id": next_id,
                "cid": checklist_id,
                "tid": it["task_template_id"],
                "sort": idx,
                "mand": 1 if it.get("is_mandatory", True) else 0,
                "proof": it.get("proof_type"),
                "staff": it.get("required_staff"),
                "emps": service.json_dump(it.get("assigned_employee_ids") or []),
                "grps": service.json_dump(it.get("assigned_group_ids") or []),
                "roles": service.json_dump(it.get("assigned_role_ids") or []),
            },
        )
        next_id += 1
    touched = await _touch_checklist(session, checklist_id=checklist_id, is_full_admin=is_full_admin)
    await session.commit()
    return touched


# --------------------------------------------------------------------------- #
# Assignees + resolution
# --------------------------------------------------------------------------- #

_GET_ASSIGNEES = text(
    """
    SELECT ca.id, ca.employee_id, ca.group_id, ca.role_id, ca.schedule_scope,
           ca.window_start, ca.window_end,
           e.name AS employee_first, e.surname AS employee_last,
           g.name AS group_name, jr.name AS role_name
    FROM checklist_assignees ca
    LEFT JOIN employees e ON e.id = ca.employee_id
    LEFT JOIN employee_groups g ON g.id = ca.group_id
    LEFT JOIN job_roles jr ON jr.id = ca.role_id
    WHERE ca.checklist_id = :cid ORDER BY ca.id
    """
)

_INSERT_ASSIGNEE = text(
    """
    INSERT INTO checklist_assignees (
        id, checklist_id, employee_id, group_id, role_id, schedule_scope,
        window_start, window_end, created_at
    ) VALUES (
        :id, :cid, :eid, :gid, :rid, :scope, :ws, :we, :created_at
    )
    """
)


async def get_assignees(session: AsyncSession, *, checklist_id: int) -> list[dict]:
    rows = (await session.execute(_GET_ASSIGNEES, {"cid": checklist_id})).mappings().all()
    out = []
    for r in rows:
        name = " ".join(p for p in (r["employee_first"], r["employee_last"]) if p).strip()
        out.append(
            {
                "id": r["id"],
                "employee_id": r["employee_id"],
                "employee_name": name or None,
                "group_id": r["group_id"],
                "group_name": r["group_name"],
                "role_id": r["role_id"],
                "role_name": r["role_name"],
                "schedule_scope": r["schedule_scope"],
                "window_start": r["window_start"],
                "window_end": r["window_end"],
            }
        )
    return out


async def set_assignees(
    session: AsyncSession, *, checklist_id: int, assignees: list[dict], is_full_admin: bool
) -> dict:
    await session.execute(
        text("DELETE FROM checklist_assignees WHERE checklist_id = :cid"), {"cid": checklist_id}
    )
    next_id = await _next_id(session, "checklist_assignees")
    now = _now_iso()
    for a in assignees:
        await session.execute(
            _INSERT_ASSIGNEE,
            {
                "id": next_id,
                "cid": checklist_id,
                "eid": a.get("employee_id"),
                "gid": a.get("group_id"),
                "rid": a.get("role_id"),
                "scope": a.get("schedule_scope", "anyone"),
                "ws": a.get("window_start"),
                "we": a.get("window_end"),
                "created_at": now,
            },
        )
        next_id += 1
    touched = await _touch_checklist(session, checklist_id=checklist_id, is_full_admin=is_full_admin)
    return touched  # caller commits (may re-resolve open runs first)


# --- audience / item resolution (R12/R13/R15) -------------------------------

_GROUP_MEMBERS = text(
    "SELECT DISTINCT employee_id FROM employee_group_members WHERE group_id IN :gids"
).bindparams(bindparam("gids", expanding=True))

_ROLE_DEFAULT_MEMBERS = text(
    "SELECT DISTINCT id AS employee_id FROM employees WHERE default_role_id IN :rids"
).bindparams(bindparam("rids", expanding=True))

_SCHEDULED_SHIFTS_BY_ROLE = text(
    "SELECT DISTINCT employee_id, start_time, end_time FROM scheduled_shifts "
    "WHERE shift_date = :d AND role_id IN :rids AND status IN ('approved', 'pending')"
).bindparams(bindparam("rids", expanding=True))

_SCHEDULED_SHIFTS = text(
    "SELECT employee_id, start_time, end_time FROM scheduled_shifts "
    "WHERE shift_date = :d AND status IN ('approved', 'pending')"
)

_SCHEDULED_OVERRIDES = text(
    "SELECT DISTINCT employee_id FROM scheduled_shifts WHERE shift_date = :d"
)

_RECURRING_SHIFTS = text(
    "SELECT rs.employee_id, rs.start_time, rs.end_time "
    "FROM recurring_shifts rs JOIN employees e ON e.id = rs.employee_id "
    "WHERE rs.is_active = 1 AND e.is_stable_schedule = 1 AND rs.day_of_week = :dow "
    "AND (rs.start_date IS NULL OR rs.start_date <= :d)"
)


async def _group_members(session: AsyncSession, group_ids: list[int]) -> set[int]:
    if not group_ids:
        return set()
    rows = (await session.execute(_GROUP_MEMBERS, {"gids": list(group_ids)})).mappings().all()
    return {r["employee_id"] for r in rows}


async def _role_default_members(session: AsyncSession, role_ids: list[int]) -> set[int]:
    if not role_ids:
        return set()
    rows = (await session.execute(_ROLE_DEFAULT_MEMBERS, {"rids": list(role_ids)})).mappings().all()
    return {r["employee_id"] for r in rows}


async def _scheduled_role_employees(
    session: AsyncSession, d: str, role_ids: list[int], win_start: str | None, win_end: str | None
) -> set[int]:
    if not role_ids:
        return set()
    out: set[int] = set()
    for r in (
        await session.execute(_SCHEDULED_SHIFTS_BY_ROLE, {"d": d, "rids": list(role_ids)})
    ).mappings().all():
        if service.windows_overlap(r["start_time"], r["end_time"], win_start, win_end):
            out.add(r["employee_id"])
    return out


async def _scheduled_employees(
    session: AsyncSession, d: str, win_start: str | None, win_end: str | None
) -> set[int]:
    out: set[int] = set()
    for r in (await session.execute(_SCHEDULED_SHIFTS, {"d": d})).mappings().all():
        if service.windows_overlap(r["start_time"], r["end_time"], win_start, win_end):
            out.add(r["employee_id"])
    overrides = {
        r["employee_id"]
        for r in (await session.execute(_SCHEDULED_OVERRIDES, {"d": d})).mappings().all()
    }
    dow = service.day_of_week(date.fromisoformat(d))
    for r in (await session.execute(_RECURRING_SHIFTS, {"dow": dow, "d": d})).mappings().all():
        if r["employee_id"] in overrides:
            continue
        if service.windows_overlap(r["start_time"], r["end_time"], win_start, win_end):
            out.add(r["employee_id"])
    return out


_CHECKLIST_ASSIGNEES = text(
    "SELECT employee_id, group_id, role_id, schedule_scope, window_start, window_end "
    "FROM checklist_assignees WHERE checklist_id = :cid ORDER BY id"
)


async def resolve_checklist_audience(
    session: AsyncSession, *, checklist_id: int, d: str
) -> set[int]:
    rows = (await session.execute(_CHECKLIST_ASSIGNEES, {"cid": checklist_id})).mappings().all()
    audience: set[int] = set()
    sched_cache: dict[tuple[Any, Any], set[int]] = {}
    for r in rows:
        if r["schedule_scope"] == "scheduled":
            key = (r["window_start"], r["window_end"])
            if key not in sched_cache:
                sched_cache[key] = await _scheduled_employees(session, d, key[0], key[1])
            sched = sched_cache[key]
            if r["employee_id"] is not None:
                if r["employee_id"] in sched:
                    audience.add(r["employee_id"])
            elif r["group_id"] is not None:
                audience |= (await _group_members(session, [r["group_id"]])) & sched
            elif r["role_id"] is not None:
                audience |= await _scheduled_role_employees(
                    session, d, [r["role_id"]], key[0], key[1]
                )
            else:
                audience |= sched
        else:
            if r["employee_id"] is not None:
                audience.add(r["employee_id"])
            elif r["group_id"] is not None:
                audience |= await _group_members(session, [r["group_id"]])
            elif r["role_id"] is not None:
                audience |= await _role_default_members(session, [r["role_id"]])
    if not rows:
        cl = await get_checklist_lean(session, checklist_id=checklist_id)
        if cl and cl["group_id"] is not None:
            audience |= await _group_members(session, [cl["group_id"]])
    return audience


_CHECKLIST_ITEMS_RESOLVE = text(
    """
    SELECT ci.task_template_id, ci.sort_order, ci.is_mandatory, ci.proof_type,
           ci.required_staff, ci.assigned_employee_ids, ci.assigned_group_ids,
           ci.assigned_role_ids,
           t.title, t.description, t.photos, t.subtasks, t.estimated_minutes,
           t.default_proof_type, t.required_staff AS tpl_required_staff,
           t.is_bonus, t.bonus_amount
    FROM checklist_items ci
    JOIN task_templates t ON t.id = ci.task_template_id
    WHERE ci.checklist_id = :cid AND t.is_active = 1
    ORDER BY ci.sort_order
    """
)


async def _resolve_items(session: AsyncSession, *, checklist_id: int) -> list[dict]:
    rows = (await session.execute(_CHECKLIST_ITEMS_RESOLVE, {"cid": checklist_id})).mappings().all()
    deps_by_tpl: dict[int, list[int]] = {}
    for dd in (await session.execute(_ALL_DEPS)).mappings().all():
        deps_by_tpl.setdefault(dd["task_template_id"], []).append(dd["depends_on_id"])

    items = []
    for r in rows:
        emp_ids = set(service.json_load(r["assigned_employee_ids"], []))
        grp_ids = service.json_load(r["assigned_group_ids"], [])
        if grp_ids:
            emp_ids |= await _group_members(session, grp_ids)
        role_ids = service.json_load(r["assigned_role_ids"], [])
        if role_ids:
            emp_ids |= await _role_default_members(session, role_ids)
        items.append(
            {
                "task_template_id": r["task_template_id"],
                "sort_order": r["sort_order"],
                "title": r["title"],
                "description": r["description"],
                "photos": service.json_load(r["photos"], []),
                "subtasks": service.json_load(r["subtasks"], []),
                "estimated_minutes": r["estimated_minutes"],
                "is_mandatory": bool(r["is_mandatory"]),
                "proof_type": r["proof_type"] or r["default_proof_type"] or "check",
                "required_staff": r["required_staff"] or r["tpl_required_staff"] or 1,
                "is_bonus": bool(r["is_bonus"]),
                "bonus_amount": _float_or_none(r["bonus_amount"]),
                "depends_on_template_ids": sorted(deps_by_tpl.get(r["task_template_id"], [])),
                "visible_to_employee_ids": sorted(emp_ids),
            }
        )
    return items


_EMPLOYEES_BY_IDS = text(
    "SELECT id, name, surname FROM employees WHERE id IN :ids ORDER BY name, surname"
).bindparams(bindparam("ids", expanding=True))


async def resolve_preview(session: AsyncSession, *, checklist_id: int, d: str) -> list[dict]:
    audience = await resolve_checklist_audience(session, checklist_id=checklist_id, d=d)
    for item in await _resolve_items(session, checklist_id=checklist_id):
        audience |= set(item["visible_to_employee_ids"])
    if not audience:
        return []
    rows = (await session.execute(_EMPLOYEES_BY_IDS, {"ids": sorted(audience)})).mappings().all()
    return [{"id": r["id"], "name": r["name"], "surname": r["surname"]} for r in rows]


# --------------------------------------------------------------------------- #
# Run generation
# --------------------------------------------------------------------------- #

_RUN_EXISTS = text(
    "SELECT id FROM checklist_runs WHERE venue_id = :v AND checklist_id = :cid AND run_date = :d"
)

_INSERT_RUN = text(
    """
    INSERT INTO checklist_runs (
        id, venue_id, checklist_id, run_date, status, name_snapshot, category,
        due_time, generated_by, created_at
    ) VALUES (
        :id, :v, :cid, :d, 'open', :name, :cat, :due, :by, :created_at
    )
    """
)

_INSERT_RUN_TASK = text(
    """
    INSERT INTO checklist_run_tasks (
        id, venue_id, run_id, task_template_id, sort_order, title, description,
        photos, estimated_minutes, is_mandatory, proof_type, required_staff,
        is_bonus, bonus_amount, depends_on_template_ids, visible_to_employee_ids,
        status, subtasks, subtasks_done
    ) VALUES (
        :id, :v, :rid, :tid, :sort, :title, :desc,
        :photos, :mins, :mand, :proof, :staff,
        :bonus, CAST(:amt AS NUMERIC), :deps, :vis,
        'pending', :subtasks, :subtasks_done
    )
    """
)

_INSERT_RUN_ASSIGNEE = text(
    "INSERT INTO checklist_run_assignees (id, venue_id, run_id, employee_id) "
    "VALUES (:id, :v, :rid, :eid)"
)


async def generate_run(
    session: AsyncSession, *, checklist: dict, d: str, generated_by: str, venue_id: str
) -> int | None:
    """Create one run of *checklist* for date *d* with content + audience snapshots.
    Returns the run id, or ``None`` when a run already exists for (venue, checklist, date)."""
    existing = (
        await session.execute(
            _RUN_EXISTS, {"v": venue_id, "cid": checklist["id"], "d": d}
        )
    ).first()
    if existing is not None:
        return None

    run_id = await _next_id(session, "checklist_runs")
    await session.execute(
        _INSERT_RUN,
        {
            "id": run_id,
            "v": venue_id,
            "cid": checklist["id"],
            "d": d,
            "name": checklist["name"],
            "cat": checklist["category"],
            "due": checklist["due_time"],
            "by": generated_by,
            "created_at": _now_iso(),
        },
    )

    audience = await resolve_checklist_audience(session, checklist_id=checklist["id"], d=d)
    items = await _resolve_items(session, checklist_id=checklist["id"])
    task_id = await _next_id(session, "checklist_run_tasks")
    for item in items:
        audience |= set(item["visible_to_employee_ids"])
        subtasks = item.get("subtasks") or []
        await session.execute(
            _INSERT_RUN_TASK,
            {
                "id": task_id,
                "v": venue_id,
                "rid": run_id,
                "tid": item["task_template_id"],
                "sort": item["sort_order"],
                "title": item["title"],
                "desc": item["description"],
                "photos": service.json_dump(item["photos"]),
                "mins": item["estimated_minutes"],
                "mand": 1 if item["is_mandatory"] else 0,
                "proof": item["proof_type"],
                "staff": item["required_staff"],
                "bonus": 1 if item["is_bonus"] else 0,
                "amt": _num_str(item["bonus_amount"]),
                "deps": service.json_dump(item["depends_on_template_ids"]),
                "vis": service.json_dump(item["visible_to_employee_ids"]),
                "subtasks": service.json_dump(subtasks),
                "subtasks_done": service.json_dump([False] * len(subtasks)),
            },
        )
        task_id += 1

    assignee_id = await _next_id(session, "checklist_run_assignees")
    for eid in sorted(audience):
        await session.execute(
            _INSERT_RUN_ASSIGNEE,
            {"id": assignee_id, "v": venue_id, "rid": run_id, "eid": eid},
        )
        assignee_id += 1
    return run_id


async def generate_pass(
    session: AsyncSession, *, d: str, generated_by: str, venue_id: str
) -> dict:
    """The daily pass: expire prior still-open runs (R21), prune runs older than
    2 years (R39), then generate one run per approved+active checklist whose
    recurrence includes *d*'s weekday. DEFERRED cron caller — no HTTP endpoint yet."""
    expired = await session.execute(
        text(
            "UPDATE checklist_runs SET status = 'expired' "
            "WHERE venue_id = :v AND run_date < :d AND status = 'open'"
        ),
        {"v": venue_id, "d": d},
    )
    prune_before = (date.fromisoformat(d) - timedelta(days=730)).isoformat()
    pruned = await session.execute(
        text("DELETE FROM checklist_runs WHERE venue_id = :v AND run_date < :b"),
        {"v": venue_id, "b": prune_before},
    )

    checklists = (
        await session.execute(
            text(
                "SELECT id, name, group_id, category, due_time, recurrence "
                "FROM checklists WHERE is_active = 1 AND approval_status = 'approved' "
                "AND is_adhoc = 0 ORDER BY id"
            )
        )
    ).mappings().all()

    generated, skipped = 0, 0
    for cl in checklists:
        if not service.recurrence_includes(cl["recurrence"], date.fromisoformat(d)):
            continue
        run_id = await generate_run(
            session, checklist=dict(cl), d=d, generated_by=generated_by, venue_id=venue_id
        )
        if run_id is not None:
            generated += 1
        else:
            skipped += 1
    return {
        "date": d,
        "generated": generated,
        "skipped_existing": skipped,
        "expired": int(getattr(expired, "rowcount", 0) or 0),
        "pruned": int(getattr(pruned, "rowcount", 0) or 0),
    }


# --------------------------------------------------------------------------- #
# Quick assign (single ad-hoc task, reuses the whole run machinery)
# --------------------------------------------------------------------------- #

_INSERT_ADHOC_CHECKLIST = text(
    """
    INSERT INTO checklists (
        id, name, category, is_active, due_time, created_by, approval_status,
        approved_by, approved_at, activated_at, updated_at, is_adhoc, created_at
    ) VALUES (
        :id, :name, :cat, 1, :due, :by, 'approved', :by, :now, :now, :now, 1, :now
    )
    """
)

_ADHOC_CANDIDATES = text(
    """
    SELECT r.id AS run_id, r.checklist_id, t.id AS run_task_id,
           t.visible_to_employee_ids,
           (SELECT COUNT(*) FROM checklist_run_tasks tt WHERE tt.run_id = r.id) AS task_count
    FROM checklist_runs r
    JOIN checklists c ON c.id = r.checklist_id AND c.is_adhoc = 1
    JOIN checklist_run_tasks t ON t.run_id = r.id
    WHERE r.venue_id = :v AND r.run_date = :d AND r.status != 'expired'
      AND t.task_template_id = :tid
    ORDER BY r.id DESC
    """
)


async def quick_assign(
    session: AsyncSession,
    *,
    task_template_id: int,
    employee_ids: list[int],
    d: str,
    due_time: str | None,
    is_mandatory: bool,
    actor: str | None,
    venue_id: str,
) -> dict:
    """Assign a single library task to specific employees for date *d* WITHOUT a
    visible checklist, reusing the whole run machinery. Idempotent: an identical
    unexpired ad-hoc run for the same (task, employees, date, venue) is returned
    unchanged (runs are immutable snapshots)."""
    emps = sorted(set(employee_ids))
    if not emps:
        raise HTTPException(status_code=400, detail="employee_ids must contain at least one employee")

    tpl = (
        await session.execute(
            text(
                "SELECT id, title, is_active, is_bonus, bonus_amount, category "
                "FROM task_templates WHERE id = :tid"
            ),
            {"tid": task_template_id},
        )
    ).mappings().first()
    if tpl is None:
        raise HTTPException(status_code=404, detail="task not found")
    if not tpl["is_active"]:
        raise HTTPException(status_code=400, detail="this task is archived")

    for cand in (
        await session.execute(_ADHOC_CANDIDATES, {"v": venue_id, "d": d, "tid": task_template_id})
    ).mappings().all():
        if cand["task_count"] != 1:
            continue
        if sorted(service.json_load(cand["visible_to_employee_ids"], [])) == emps:
            return {
                "run_id": cand["run_id"],
                "run_task_id": cand["run_task_id"],
                "checklist_id": cand["checklist_id"],
                "date": d,
                "assigned_employee_ids": emps,
                "already_existed": True,
            }

    checklist_id = await _next_id(session, "checklists")
    now = _now_iso()
    cat = tpl["category"] if tpl["category"] in service.VALID_TASK_CATEGORIES else "during_shift"
    await session.execute(
        _INSERT_ADHOC_CHECKLIST,
        {
            "id": checklist_id,
            "name": f"Quick: {tpl['title']}"[:200],
            "cat": cat,
            "due": due_time,
            "by": actor,
            "now": now,
        },
    )
    item_id = await _next_id(session, "checklist_items")
    await session.execute(
        _INSERT_ITEM,
        {
            "id": item_id,
            "cid": checklist_id,
            "tid": task_template_id,
            "sort": 0,
            "mand": 1 if is_mandatory else 0,
            "proof": None,
            "staff": None,
            "emps": service.json_dump(emps),
            "grps": service.json_dump([]),
            "roles": service.json_dump([]),
        },
    )

    checklist = await get_checklist_lean(session, checklist_id=checklist_id)
    assert checklist is not None  # noqa: S101 - just inserted above
    run_id = await generate_run(
        session, checklist=dict(checklist), d=d, generated_by=f"quick:{actor}", venue_id=venue_id
    )
    if run_id is None:
        raise HTTPException(status_code=409, detail="a run for this ad-hoc checklist already exists")
    run_task = (
        await session.execute(
            text("SELECT id FROM checklist_run_tasks WHERE run_id = :rid ORDER BY id LIMIT 1"),
            {"rid": run_id},
        )
    ).mappings().first()
    return {
        "run_id": run_id,
        "run_task_id": run_task["id"] if run_task else None,
        "checklist_id": checklist_id,
        "date": d,
        "assigned_employee_ids": emps,
        "already_existed": False,
    }


# --------------------------------------------------------------------------- #
# Run reads
# --------------------------------------------------------------------------- #

_GET_RUN = text(
    "SELECT id, venue_id, checklist_id, run_date, status, name_snapshot, category, "
    "due_time, generated_by, created_at, completed_at FROM checklist_runs WHERE id = :rid"
)

_LIST_RUNS = text(
    """
    SELECT r.id, r.checklist_id, r.run_date, r.status, r.name_snapshot, r.category,
           r.due_time, r.generated_by, r.created_at, r.completed_at,
           (SELECT COUNT(*) FROM checklist_run_tasks t WHERE t.run_id = r.id) AS total_tasks,
           (SELECT COUNT(*) FROM checklist_run_tasks t WHERE t.run_id = r.id AND t.status = 'done') AS done_tasks,
           (SELECT COUNT(*) FROM checklist_run_tasks t WHERE t.run_id = r.id AND t.is_mandatory = 1) AS mandatory_total,
           (SELECT COUNT(*) FROM checklist_run_tasks t WHERE t.run_id = r.id AND t.is_mandatory = 1 AND t.status = 'done') AS mandatory_done,
           (SELECT COUNT(*) FROM checklist_run_assignees a WHERE a.run_id = r.id) AS assignee_count,
           COALESCE(c.is_adhoc, 0) AS is_adhoc
    FROM checklist_runs r
    LEFT JOIN checklists c ON c.id = r.checklist_id
    WHERE r.venue_id = :v
      AND (CAST(:f AS TEXT) IS NULL OR r.run_date >= CAST(:f AS TEXT))
      AND (CAST(:t AS TEXT) IS NULL OR r.run_date <= CAST(:t AS TEXT))
      AND (CAST(:cid AS INTEGER) IS NULL OR r.checklist_id = CAST(:cid AS INTEGER))
      AND (CAST(:status AS TEXT) IS NULL OR r.status = CAST(:status AS TEXT))
      AND (CAST(:eid AS INTEGER) IS NULL OR EXISTS (
            SELECT 1 FROM checklist_run_assignees a
            WHERE a.run_id = r.id AND a.employee_id = CAST(:eid AS INTEGER)))
    ORDER BY r.run_date DESC, r.name_snapshot
    """
)


async def list_runs(
    session: AsyncSession,
    *,
    venue_id: str,
    date_from: str | None,
    date_to: str | None,
    checklist_id: int | None,
    employee_id: int | None,
    status: str | None,
) -> list[dict]:
    rows = (
        await session.execute(
            _LIST_RUNS,
            {
                "v": venue_id,
                "f": date_from,
                "t": date_to,
                "cid": checklist_id,
                "status": status,
                "eid": employee_id,
            },
        )
    ).mappings().all()
    today = date.today().isoformat()
    now_hm = datetime.now().strftime("%H:%M")
    out = []
    for r in rows:
        overdue = bool(
            r["status"] == "open"
            and r["mandatory_done"] < r["mandatory_total"]
            and r["due_time"] is not None
            and (r["run_date"] < today or (r["run_date"] == today and now_hm > r["due_time"]))
        )
        out.append(
            {
                "id": r["id"],
                "checklist_id": r["checklist_id"],
                "run_date": r["run_date"],
                "status": r["status"],
                "name": r["name_snapshot"],
                "category": r["category"],
                "due_time": r["due_time"],
                "generated_by": r["generated_by"],
                "created_at": r["created_at"],
                "completed_at": r["completed_at"],
                "total_tasks": r["total_tasks"],
                "done_tasks": r["done_tasks"],
                "mandatory_total": r["mandatory_total"],
                "mandatory_done": r["mandatory_done"],
                "assignee_count": r["assignee_count"],
                "overdue": overdue,
                "is_adhoc": bool(r["is_adhoc"]),
            }
        )
    return out


_RUN_TASKS = text(
    "SELECT id, run_id, task_template_id, sort_order, title, description, photos, "
    "estimated_minutes, is_mandatory, proof_type, required_staff, is_bonus, "
    "bonus_amount, depends_on_template_ids, visible_to_employee_ids, status, "
    "subtasks, subtasks_done FROM checklist_run_tasks WHERE run_id = :rid "
    "ORDER BY sort_order, id"
)

_RUN_PARTICIPANTS = text(
    """
    SELECT p.run_task_id, p.employee_id, p.started_at, p.finished_at, p.proof_text,
           p.proof_photos, e.name AS first, e.surname AS last
    FROM checklist_run_task_participants p
    JOIN employees e ON e.id = p.employee_id
    JOIN checklist_run_tasks t ON t.id = p.run_task_id
    WHERE t.run_id = :rid ORDER BY p.started_at, p.id
    """
)


def _full_name(first: Any, last: Any) -> str:
    return " ".join(p for p in (first, last) if p).strip()


async def _run_tasks_with_participants(session: AsyncSession, *, run_id: int) -> list[dict]:
    tasks = (await session.execute(_RUN_TASKS, {"rid": run_id})).mappings().all()
    parts = (await session.execute(_RUN_PARTICIPANTS, {"rid": run_id})).mappings().all()
    by_task: dict[int, list[dict]] = {}
    for p in parts:
        by_task.setdefault(p["run_task_id"], []).append(
            {
                "employee_id": p["employee_id"],
                "name": _full_name(p["first"], p["last"]),
                "started_at": p["started_at"],
                "finished_at": p["finished_at"],
                "proof_text": p["proof_text"],
                "proof_photos": service.json_load(p["proof_photos"], []),
            }
        )
    out = []
    for t in tasks:
        out.append(
            {
                "run_task_id": t["id"],
                "task_template_id": t["task_template_id"],
                "sort_order": t["sort_order"],
                "title": t["title"],
                "description": t["description"],
                "photos": service.json_load(t["photos"], []),
                "estimated_minutes": t["estimated_minutes"],
                "is_mandatory": bool(t["is_mandatory"]),
                "proof_type": t["proof_type"],
                "required_staff": t["required_staff"],
                "is_bonus": bool(t["is_bonus"]),
                "bonus_amount": _float_or_none(t["bonus_amount"]),
                "depends_on_template_ids": service.json_load(t["depends_on_template_ids"], []),
                "visible_to_employee_ids": service.json_load(t["visible_to_employee_ids"], []),
                "status": t["status"],
                "subtasks": service.json_load(t["subtasks"], []),
                "subtasks_done": service.json_load(t["subtasks_done"], []),
                "participants": by_task.get(t["id"], []),
            }
        )
    return out


async def get_run_detail(session: AsyncSession, *, run_id: int) -> dict:
    """Admin/manager full view — includes staff-hidden tasks and the audit trail."""
    run = (await session.execute(_GET_RUN, {"rid": run_id})).mappings().first()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    adhoc = (
        await session.execute(
            text("SELECT COALESCE(is_adhoc, 0) AS is_adhoc FROM checklists WHERE id = :cid"),
            {"cid": run["checklist_id"]},
        )
    ).mappings().first()
    tasks = await _run_tasks_with_participants(session, run_id=run_id)
    assignees = (
        await session.execute(
            text(
                "SELECT a.employee_id, e.name, e.surname FROM checklist_run_assignees a "
                "JOIN employees e ON e.id = a.employee_id WHERE a.run_id = :rid "
                "ORDER BY e.name, e.surname"
            ),
            {"rid": run_id},
        )
    ).mappings().all()
    events = (
        await session.execute(
            text(
                "SELECT ev.id, ev.run_task_id, ev.employee_id, ev.event, ev.payload, "
                "ev.created_at, e.name AS first, e.surname AS last "
                "FROM checklist_run_events ev "
                "JOIN checklist_run_tasks t ON t.id = ev.run_task_id "
                "LEFT JOIN employees e ON e.id = ev.employee_id "
                "WHERE t.run_id = :rid ORDER BY ev.created_at, ev.id"
            ),
            {"rid": run_id},
        )
    ).mappings().all()
    return {
        "run": {
            "id": run["id"],
            "checklist_id": run["checklist_id"],
            "run_date": run["run_date"],
            "status": run["status"],
            "name": run["name_snapshot"],
            "category": run["category"],
            "due_time": run["due_time"],
            "generated_by": run["generated_by"],
            "created_at": run["created_at"],
            "completed_at": run["completed_at"],
            "is_adhoc": bool(adhoc and adhoc["is_adhoc"]),
        },
        "assignees": [
            {"employee_id": a["employee_id"], "name": a["name"], "surname": a["surname"]}
            for a in assignees
        ],
        "tasks": tasks,
        "events": [
            {
                "id": ev["id"],
                "run_task_id": ev["run_task_id"],
                "employee_id": ev["employee_id"],
                "employee_name": _full_name(ev["first"], ev["last"]) or None,
                "event": ev["event"],
                "payload": service.json_load(ev["payload"], {}),
                "created_at": ev["created_at"],
            }
            for ev in events
        ],
    }


# --------------------------------------------------------------------------- #
# Staff reads (ownership-gated)
# --------------------------------------------------------------------------- #

_IS_RUN_ASSIGNEE = text(
    "SELECT 1 FROM checklist_run_assignees WHERE run_id = :rid AND employee_id = :eid"
)


def _staff_task_shape(task: dict, eid: int, gated: bool, dep_titles: list[str]) -> dict:
    mine = next((p for p in task["participants"] if p["employee_id"] == eid), None)
    return {
        "run_task_id": task["run_task_id"],
        "title": task["title"],
        "description": task["description"],
        "photos": task["photos"],
        "estimated_minutes": task["estimated_minutes"],
        "is_mandatory": task["is_mandatory"],
        "proof_type": task["proof_type"],
        "required_staff": task["required_staff"],
        "is_bonus": task["is_bonus"],
        "bonus_amount": task["bonus_amount"],
        "sort_order": task["sort_order"],
        "status": task["status"],
        "subtasks": task.get("subtasks", []),
        "subtasks_done": task.get("subtasks_done", []),
        "assigned_to_me": bool(task["visible_to_employee_ids"]),
        "locked": bool(gated and not task["is_mandatory"] and task["status"] == "pending"),
        "blocked_by": dep_titles,
        "participants": [
            {
                "employee_id": p["employee_id"],
                "name": p["name"],
                "started_at": p["started_at"],
                "finished_at": p["finished_at"],
            }
            for p in task["participants"]
        ],
        "mine": {
            "started": mine is not None,
            "finished": bool(mine and mine["finished_at"]),
        },
    }


def _staff_run_shape(run: dict, tasks: list[dict], eid: int, gated: bool | None = None) -> dict:
    visible = [
        t for t in tasks
        if not t["visible_to_employee_ids"] or eid in t["visible_to_employee_ids"]
    ]
    if gated is None:
        gated = any(t["is_mandatory"] and t["status"] == "pending" for t in tasks)
    status_by_template = {
        t["task_template_id"]: t["status"] for t in tasks if t["task_template_id"]
    }
    title_by_template = {
        t["task_template_id"]: t["title"] for t in tasks if t["task_template_id"]
    }
    shaped = []
    for t in visible:
        dep_titles = [
            title_by_template[d]
            for d in (t["depends_on_template_ids"] or [])
            if d in status_by_template and status_by_template[d] != "done"
        ]
        shaped.append(_staff_task_shape(t, eid, gated, dep_titles))
    shaped.sort(key=lambda t: (0 if t["is_mandatory"] else 1, t["sort_order"], t["run_task_id"]))
    done = sum(1 for t in shaped if t["status"] == "done")
    mandatory_open = sum(1 for t in shaped if t["is_mandatory"] and t["status"] != "done")
    return {
        "run_id": run["id"],
        "checklist_id": run["checklist_id"],
        "run_date": run["run_date"],
        "name": run["name_snapshot"],
        "category": run["category"],
        "due_time": run["due_time"],
        "status": run["status"],
        "progress": {"total": len(shaped), "done": done, "mandatory_open": mandatory_open},
        "tasks": shaped,
    }


_STAFF_RUNS = text(
    """
    SELECT r.id, r.checklist_id, r.run_date, r.status, r.name_snapshot, r.category, r.due_time
    FROM checklist_runs r
    JOIN checklist_run_assignees a ON a.run_id = r.id AND a.employee_id = :eid
    WHERE r.venue_id = :v AND (r.run_date = :d OR (r.run_date = :prev AND r.status != 'expired'))
    ORDER BY r.run_date DESC, r.name_snapshot
    """
)


async def get_staff_runs(
    session: AsyncSession, *, employee_id: int, d: str, venue_id: str
) -> list[dict]:
    """Runs the caller is assigned to for day *d* plus a still-open previous-day run
    (operating-day window). Ownership: only runs where *employee_id* is a run assignee."""
    prev = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
    runs = (
        await session.execute(_STAFF_RUNS, {"eid": employee_id, "v": venue_id, "d": d, "prev": prev})
    ).mappings().all()
    run_tasks = [
        (dict(run), await _run_tasks_with_participants(session, run_id=run["id"]))
        for run in runs
    ]
    cross_gated = any(
        t["is_mandatory"] and t["status"] == "pending"
        for _run, tasks in run_tasks for t in tasks
    )
    return [_staff_run_shape(run, tasks, employee_id, cross_gated) for run, tasks in run_tasks]


async def get_staff_run(session: AsyncSession, *, employee_id: int, run_id: int) -> dict:
    run = (await session.execute(_GET_RUN, {"rid": run_id})).mappings().first()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    member = (
        await session.execute(_IS_RUN_ASSIGNEE, {"rid": run_id, "eid": employee_id})
    ).first()
    if member is None:
        raise HTTPException(status_code=403, detail="not_assigned: you are not assigned to this checklist")
    tasks = await _run_tasks_with_participants(session, run_id=run_id)
    return _staff_run_shape(dict(run), tasks, employee_id)


async def staff_task_response(session: AsyncSession, *, run_task_id: int, employee_id: int) -> dict:
    row = (
        await session.execute(
            text("SELECT run_id FROM checklist_run_tasks WHERE id = :tid"), {"tid": run_task_id}
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    run = (await session.execute(_GET_RUN, {"rid": row["run_id"]})).mappings().first()
    assert run is not None  # noqa: S101 - task's parent run always exists
    tasks = await _run_tasks_with_participants(session, run_id=row["run_id"])
    shaped = _staff_run_shape(dict(run), tasks, employee_id)
    task = next((t for t in shaped["tasks"] if t["run_task_id"] == run_task_id), None)
    return {"ok": True, "task": task, "run_status": run["status"]}


# --------------------------------------------------------------------------- #
# Shared action guards + events
# --------------------------------------------------------------------------- #

_GET_TASK = text(
    "SELECT t.id, t.venue_id, t.run_id, t.task_template_id, t.title, t.is_mandatory, "
    "t.proof_type, t.required_staff, t.is_bonus, t.bonus_amount, "
    "t.depends_on_template_ids, t.visible_to_employee_ids, t.status, "
    "r.status AS run_status, r.run_date "
    "FROM checklist_run_tasks t JOIN checklist_runs r ON r.id = t.run_id WHERE t.id = :tid"
)

_TASK_PARTICIPANTS = text(
    "SELECT p.id, p.employee_id, p.started_at, p.finished_at, p.proof_text, p.proof_photos, "
    "e.name AS first, e.surname AS last "
    "FROM checklist_run_task_participants p JOIN employees e ON e.id = p.employee_id "
    "WHERE p.run_task_id = :tid ORDER BY p.started_at, p.id"
)

_INSERT_EVENT = text(
    "INSERT INTO checklist_run_events (id, venue_id, run_task_id, employee_id, event, payload, created_at) "
    "VALUES (:id, :v, :tid, :eid, :event, :payload, :now)"
)

_SET_TASK_STATUS = text("UPDATE checklist_run_tasks SET status = :status WHERE id = :tid")


async def _log_event(
    session: AsyncSession,
    *,
    venue_id: str,
    run_task_id: int,
    employee_id: int | None,
    event: str,
    payload: dict,
) -> None:
    ev_id = await _next_id(session, "checklist_run_events")
    await session.execute(
        _INSERT_EVENT,
        {
            "id": ev_id,
            "v": venue_id,
            "tid": run_task_id,
            "eid": employee_id,
            "event": event,
            "payload": service.json_dump(payload),
            "now": _now_iso(),
        },
    )


async def _participants(session: AsyncSession, run_task_id: int) -> list[dict]:
    rows = (await session.execute(_TASK_PARTICIPANTS, {"tid": run_task_id})).mappings().all()
    out = []
    for p in rows:
        d = dict(p)
        d["name"] = _full_name(p["first"], p["last"])
        out.append(d)
    return out


def _first_name(participants: list[dict]) -> str | None:
    return participants[0]["name"] if participants else None


async def _load_task_checked(
    session: AsyncSession, *, run_task_id: int, employee_id: int, clocked_in: bool
) -> dict:
    """Enforce the staff guards in contract order:
    404 unknown/invisible -> 403 not_assigned -> 403 not_clocked_in -> 409 expired."""
    task = (await session.execute(_GET_TASK, {"tid": run_task_id})).mappings().first()
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    member = (
        await session.execute(_IS_RUN_ASSIGNEE, {"rid": task["run_id"], "eid": employee_id})
    ).first()
    if member is None:
        raise HTTPException(status_code=403, detail="not_assigned: you are not assigned to this checklist")
    visible = service.json_load(task["visible_to_employee_ids"], [])
    if visible and employee_id not in visible:
        raise HTTPException(status_code=404, detail="task not found")
    if not clocked_in:
        raise HTTPException(status_code=403, detail="not_clocked_in: clock in to start working on tasks")
    if task["run_status"] == "expired":
        raise HTTPException(status_code=409, detail="conflict: this run has expired")
    return dict(task)


async def _load_task_admin(session: AsyncSession, *, run_task_id: int) -> dict:
    task = (await session.execute(_GET_TASK, {"tid": run_task_id})).mappings().first()
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return dict(task)


# --------------------------------------------------------------------------- #
# Run completion / reopen helpers
# --------------------------------------------------------------------------- #


async def _maybe_complete_run(session: AsyncSession, *, run_id: int) -> None:
    """R21: run completes when the last mandatory task is done."""
    counts = (
        await session.execute(
            text(
                "SELECT SUM(CASE WHEN is_mandatory = 1 THEN 1 ELSE 0 END) AS mand_total, "
                "SUM(CASE WHEN is_mandatory = 1 AND status != 'done' THEN 1 ELSE 0 END) AS mand_open "
                "FROM checklist_run_tasks WHERE run_id = :rid"
            ),
            {"rid": run_id},
        )
    ).mappings().first()
    mand_total = (counts["mand_total"] if counts else 0) or 0
    mand_open = (counts["mand_open"] if counts else 0) or 0
    if mand_total > 0 and mand_open == 0:
        await session.execute(
            text(
                "UPDATE checklist_runs SET status = 'completed', completed_at = :now "
                "WHERE id = :rid AND status = 'open'"
            ),
            {"rid": run_id, "now": _now_iso()},
        )


async def _reopen_run_if_needed(session: AsyncSession, *, run_id: int) -> None:
    await session.execute(
        text(
            "UPDATE checklist_runs SET status = 'open', completed_at = NULL "
            "WHERE id = :rid AND status = 'completed'"
        ),
        {"rid": run_id},
    )


async def _load_subtasks(session: AsyncSession, run_task_id: int) -> dict:
    row = (
        await session.execute(
            text("SELECT subtasks, subtasks_done FROM checklist_run_tasks WHERE id = :tid"),
            {"tid": run_task_id},
        )
    ).mappings().first()
    subtasks = service.json_load(row["subtasks"], []) if row else []
    done = service.json_load(row["subtasks_done"], []) if row else []
    done = [bool(x) for x in done][: len(subtasks)]
    if len(done) < len(subtasks):
        done += [False] * (len(subtasks) - len(done))
    return {"subtasks": subtasks, "subtasks_done": done}


# --------------------------------------------------------------------------- #
# Staff actions (ownership + clock-in enforced via _load_task_checked)
# --------------------------------------------------------------------------- #

_INSERT_PARTICIPANT = text(
    "INSERT INTO checklist_run_task_participants "
    "(id, venue_id, run_task_id, employee_id, started_at) VALUES (:id, :v, :tid, :eid, :now)"
)

_UNMET_DEPS = text(
    "SELECT title FROM checklist_run_tasks "
    "WHERE run_id = :rid AND task_template_id IN :tids AND status != 'done'"
).bindparams(bindparam("tids", expanding=True))

_CROSS_RUN_MANDATORY_PENDING = text(
    """
    SELECT EXISTS(
        SELECT 1 FROM checklist_run_tasks t
        JOIN checklist_runs r ON r.id = t.run_id
        JOIN checklist_run_assignees a ON a.run_id = r.id AND a.employee_id = :eid
        WHERE r.status = 'open'
          AND r.run_date = (SELECT run_date FROM checklist_runs WHERE id = :rid)
          AND t.is_mandatory = 1 AND t.status = 'pending'
    ) AS gated
    """
)


async def start_task(
    session: AsyncSession, *, run_task_id: int, employee_id: int, clocked_in: bool
) -> None:
    task = await _load_task_checked(
        session, run_task_id=run_task_id, employee_id=employee_id, clocked_in=clocked_in
    )
    parts = await _participants(session, run_task_id)
    if task["status"] == "done":
        raise HTTPException(status_code=409, detail=f"conflict: already done by {_first_name(parts) or 'someone else'}")
    if any(p["employee_id"] == employee_id for p in parts):
        raise HTTPException(status_code=409, detail="conflict: you already started this task")

    dep_ids = service.json_load(task["depends_on_template_ids"], [])
    if dep_ids:
        unmet = (
            await session.execute(_UNMET_DEPS, {"rid": task["run_id"], "tids": list(dep_ids)})
        ).mappings().all()
        if unmet:
            titles = [u["title"] for u in unmet]
            raise HTTPException(status_code=409, detail=f"conflict: blocked by {', '.join(titles)}")

    if not task["is_mandatory"]:
        gated = (
            await session.execute(
                _CROSS_RUN_MANDATORY_PENDING, {"eid": employee_id, "rid": task["run_id"]}
            )
        ).mappings().first()
        if gated and gated["gated"]:
            raise HTTPException(status_code=409, detail="mandatory_first: unlocks when all must-do tasks are underway")

    if len(parts) >= task["required_staff"]:
        name = _first_name(parts) or "Someone"
        if task["is_bonus"]:
            raise HTTPException(status_code=409, detail=f"bonus_owned: {name} got this bonus task")
        raise HTTPException(status_code=409, detail=f"conflict: {name} already started this task")

    part_id = await _next_id(session, "checklist_run_task_participants")
    await session.execute(
        _INSERT_PARTICIPANT,
        {"id": part_id, "v": task["venue_id"], "tid": run_task_id, "eid": employee_id, "now": _now_iso()},
    )
    if task["status"] == "pending":
        await session.execute(_SET_TASK_STATUS, {"status": "in_progress", "tid": run_task_id})
    await _log_event(
        session, venue_id=task["venue_id"], run_task_id=run_task_id, employee_id=employee_id,
        event="started", payload={},
    )
    await session.commit()


async def finish_task(
    session: AsyncSession,
    *,
    run_task_id: int,
    employee_id: int,
    clocked_in: bool,
    note: str | None,
    photo_urls: list[str],
) -> None:
    task = await _load_task_checked(
        session, run_task_id=run_task_id, employee_id=employee_id, clocked_in=clocked_in
    )
    parts = await _participants(session, run_task_id)
    if task["status"] == "done":
        raise HTTPException(status_code=409, detail=f"conflict: already done by {_first_name(parts) or 'someone else'}")
    mine = next((p for p in parts if p["employee_id"] == employee_id), None)
    if mine is None:
        raise HTTPException(status_code=409, detail="conflict: start this task before finishing it")
    if mine["finished_at"]:
        raise HTTPException(status_code=409, detail="conflict: you already finished this task")

    if task["proof_type"] == "photo" and not photo_urls:
        raise HTTPException(status_code=400, detail="photo proof is required to finish this task")
    if task["proof_type"] == "text" and len((note or "").strip()) < 3:
        raise HTTPException(status_code=400, detail="a note (at least 3 characters) is required to finish this task")

    subtasks = await _load_subtasks(session, run_task_id)
    if subtasks["subtasks"] and not all(subtasks["subtasks_done"]):
        raise HTTPException(status_code=409, detail="subtasks_incomplete: tick off all sub-steps before finishing")

    await session.execute(
        text(
            "UPDATE checklist_run_task_participants "
            "SET finished_at = :now, proof_text = :note, proof_photos = :photos "
            "WHERE run_task_id = :tid AND employee_id = :eid"
        ),
        {
            "now": _now_iso(),
            "note": (note or None),
            "photos": service.json_dump(photo_urls or []),
            "tid": run_task_id,
            "eid": employee_id,
        },
    )
    await _log_event(
        session, venue_id=task["venue_id"], run_task_id=run_task_id, employee_id=employee_id,
        event="finished", payload={"proof_type": task["proof_type"], "photo_count": len(photo_urls or [])},
    )

    finished_count = sum(1 for p in parts if p["finished_at"]) + 1
    if finished_count >= task["required_staff"]:
        await session.execute(_SET_TASK_STATUS, {"status": "done", "tid": run_task_id})
        # BONUS HOOK (payroll domain): a completed is_bonus task credits each finisher.
        # Split the bonus over the finishers (earliest first, this employee last) and
        # write one bonus_task_log row each; the payroll crud enforces the double-pay
        # guard and shares this transaction (commit=False). Notifier approval card is
        # DEFERRED. Imported lazily so tasks->payroll stays a call-time edge (no cycle).
        await _credit_bonus_task(session, task=task, parts=parts, finisher=employee_id)
        await _maybe_complete_run(session, run_id=task["run_id"])
    await session.commit()


async def _credit_bonus_task(
    session: AsyncSession, *, task: dict, parts: list[dict], finisher: int
) -> None:
    """Credit a just-completed bonus task's split across its finishers (payroll ledger)."""
    if not task.get("is_bonus") or task.get("bonus_amount") in (None, ""):
        return
    from decimal import Decimal

    from be.app.domains.payroll import crud as payroll_crud

    finisher_ids = [p["employee_id"] for p in parts if p["finished_at"]] + [finisher]
    amount = float(task["bonus_amount"])
    shares = service.split_bonus(Decimal(str(amount)), len(finisher_ids))
    for eid, share in zip(finisher_ids, shares, strict=True):
        await payroll_crud.credit_task_bonus(
            session,
            venue_id=task["venue_id"],
            run_task_id=task["id"],
            employee_id=eid,
            amount=float(share),
            task_title=task.get("title"),
            commit=False,
        )


async def unstart_task(
    session: AsyncSession, *, run_task_id: int, employee_id: int, clocked_in: bool
) -> None:
    """Release my own participation, or self-undo my finish (R23)."""
    task = await _load_task_checked(
        session, run_task_id=run_task_id, employee_id=employee_id, clocked_in=clocked_in
    )
    parts = await _participants(session, run_task_id)
    mine = next((p for p in parts if p["employee_id"] == employee_id), None)
    if mine is None:
        raise HTTPException(status_code=409, detail="conflict: you haven't started this task")

    if mine["finished_at"]:
        was_done = task["status"] == "done"
        # BONUS HOOK (payroll): reopening a completed bonus task voids every finisher's
        # LIVE payout (voided rows survive for audit; re-completion inserts fresh live
        # rows). Shares this transaction (commit=False). Imported lazily to avoid a cycle.
        if was_done and task.get("is_bonus"):
            from be.app.domains.payroll import crud as payroll_crud

            for p in parts:
                if p["finished_at"]:
                    await payroll_crud.void_task_bonus(
                        session,
                        run_task_id=task["id"],
                        employee_id=p["employee_id"],
                        commit=False,
                    )
        await _log_event(
            session, venue_id=task["venue_id"], run_task_id=run_task_id, employee_id=employee_id,
            event="reopened",
            payload={
                "by": "self",
                "prior_finished_at": mine["finished_at"],
                "prior_proof_text": mine["proof_text"],
                "prior_proof_photos": service.json_load(mine["proof_photos"], []),
            },
        )
        await session.execute(
            text(
                "UPDATE checklist_run_task_participants "
                "SET finished_at = NULL, proof_text = NULL, proof_photos = '[]' "
                "WHERE run_task_id = :tid AND employee_id = :eid"
            ),
            {"tid": run_task_id, "eid": employee_id},
        )
        if was_done:
            await session.execute(_SET_TASK_STATUS, {"status": "in_progress", "tid": run_task_id})
            if task["is_mandatory"]:
                await _reopen_run_if_needed(session, run_id=task["run_id"])
    else:
        await session.execute(
            text(
                "DELETE FROM checklist_run_task_participants "
                "WHERE run_task_id = :tid AND employee_id = :eid"
            ),
            {"tid": run_task_id, "eid": employee_id},
        )
        await _log_event(
            session, venue_id=task["venue_id"], run_task_id=run_task_id, employee_id=employee_id,
            event="released", payload={"self": True, "started_at": mine["started_at"]},
        )
        remaining = [p for p in parts if p["employee_id"] != employee_id]
        if not remaining and task["status"] == "in_progress":
            await session.execute(_SET_TASK_STATUS, {"status": "pending", "tid": run_task_id})
    await session.commit()


async def set_subtask(
    session: AsyncSession,
    *,
    run_task_id: int,
    employee_id: int,
    clocked_in: bool,
    index: int,
    done: bool,
) -> dict:
    task = await _load_task_checked(
        session, run_task_id=run_task_id, employee_id=employee_id, clocked_in=clocked_in
    )
    if task["status"] == "done":
        raise HTTPException(status_code=409, detail="conflict: this task is already done")
    if task["status"] != "in_progress":
        raise HTTPException(status_code=409, detail="not_started: start this task before ticking its sub-steps")
    mine = (
        await session.execute(
            text(
                "SELECT 1 FROM checklist_run_task_participants "
                "WHERE run_task_id = :tid AND employee_id = :eid"
            ),
            {"tid": run_task_id, "eid": employee_id},
        )
    ).first()
    if mine is None:
        raise HTTPException(status_code=409, detail="not_started: start this task before ticking its sub-steps")

    state = await _load_subtasks(session, run_task_id)
    subtasks_done = state["subtasks_done"]
    if index < 0 or index >= len(subtasks_done):
        raise HTTPException(status_code=400, detail="subtask index out of range")
    subtasks_done[index] = bool(done)
    await session.execute(
        text("UPDATE checklist_run_tasks SET subtasks_done = :done WHERE id = :tid"),
        {"done": service.json_dump(subtasks_done), "tid": run_task_id},
    )
    await session.commit()
    return {"subtasks_done": subtasks_done}


async def take_over_task(
    session: AsyncSession,
    *,
    run_task_id: int,
    employee_id: int,
    clocked_in: bool,
    from_employee_id: int | None,
) -> None:
    task = await _load_task_checked(
        session, run_task_id=run_task_id, employee_id=employee_id, clocked_in=clocked_in
    )
    if task["is_bonus"]:
        raise HTTPException(status_code=403, detail="bonus_owned: bonus tasks can't be taken over")
    parts = await _participants(session, run_task_id)
    if task["status"] == "done":
        raise HTTPException(status_code=409, detail=f"conflict: already done by {_first_name(parts) or 'someone else'}")
    if any(p["employee_id"] == employee_id for p in parts):
        raise HTTPException(status_code=409, detail="conflict: you already started this task")
    unfinished = [p for p in parts if not p["finished_at"]]
    if from_employee_id is not None:
        unfinished = [p for p in unfinished if p["employee_id"] == from_employee_id]
    if not unfinished:
        raise HTTPException(status_code=409, detail="conflict: no in-progress participant to take over from")
    target = unfinished[0]

    await session.execute(
        text(
            "DELETE FROM checklist_run_task_participants "
            "WHERE run_task_id = :tid AND employee_id = :eid"
        ),
        {"tid": run_task_id, "eid": target["employee_id"]},
    )
    await _log_event(
        session, venue_id=task["venue_id"], run_task_id=run_task_id, employee_id=target["employee_id"],
        event="released", payload={"taken_over_by": employee_id, "started_at": target["started_at"]},
    )
    part_id = await _next_id(session, "checklist_run_task_participants")
    await session.execute(
        _INSERT_PARTICIPANT,
        {"id": part_id, "v": task["venue_id"], "tid": run_task_id, "eid": employee_id, "now": _now_iso()},
    )
    await _log_event(
        session, venue_id=task["venue_id"], run_task_id=run_task_id, employee_id=employee_id,
        event="taken_over", payload={"from_employee_id": target["employee_id"], "from_name": target["name"]},
    )
    await session.commit()


# --------------------------------------------------------------------------- #
# Admin / manager run actions
# --------------------------------------------------------------------------- #


async def admin_release(
    session: AsyncSession, *, run_task_id: int, employee_id: int | None, actor: str | None
) -> int:
    task = await _load_task_admin(session, run_task_id=run_task_id)
    if task["status"] == "done":
        raise HTTPException(status_code=400, detail="task is done — use reopen instead")
    parts = await _participants(session, run_task_id)
    targets = [p for p in parts if not p["finished_at"]]
    if employee_id is not None:
        targets = [p for p in targets if p["employee_id"] == employee_id]
    if not targets:
        raise HTTPException(status_code=400, detail="no in-progress participant to release")
    for p in targets:
        await session.execute(
            text(
                "DELETE FROM checklist_run_task_participants "
                "WHERE run_task_id = :tid AND employee_id = :eid"
            ),
            {"tid": run_task_id, "eid": p["employee_id"]},
        )
        await _log_event(
            session, venue_id=task["venue_id"], run_task_id=run_task_id, employee_id=p["employee_id"],
            event="released", payload={"by": actor, "started_at": p["started_at"]},
        )
    if len(parts) - len(targets) == 0 and task["status"] == "in_progress":
        await session.execute(_SET_TASK_STATUS, {"status": "pending", "tid": run_task_id})
    await session.commit()
    return len(targets)


async def admin_reopen(
    session: AsyncSession, *, run_task_id: int, employee_id: int | None, actor: str | None
) -> int:
    task = await _load_task_admin(session, run_task_id=run_task_id)
    parts = await _participants(session, run_task_id)
    targets = [p for p in parts if p["finished_at"]]
    if employee_id is not None:
        targets = [p for p in targets if p["employee_id"] == employee_id]
    if not targets:
        raise HTTPException(status_code=400, detail="nothing to reopen")
    # BONUS HOOK (payroll, DEFERRED): a completed bonus task's payout would be voided here.
    for p in targets:
        await _log_event(
            session, venue_id=task["venue_id"], run_task_id=run_task_id, employee_id=p["employee_id"],
            event="reopened",
            payload={
                "by": actor,
                "prior_finished_at": p["finished_at"],
                "prior_proof_text": p["proof_text"],
                "prior_proof_photos": service.json_load(p["proof_photos"], []),
            },
        )
        await session.execute(
            text(
                "UPDATE checklist_run_task_participants "
                "SET finished_at = NULL, proof_text = NULL, proof_photos = '[]' "
                "WHERE run_task_id = :tid AND employee_id = :eid"
            ),
            {"tid": run_task_id, "eid": p["employee_id"]},
        )
    if task["status"] == "done":
        await session.execute(_SET_TASK_STATUS, {"status": "in_progress", "tid": run_task_id})
        if task["is_mandatory"]:
            await _reopen_run_if_needed(session, run_id=task["run_id"])
    await session.commit()
    return len(targets)


async def retro_complete(session: AsyncSession, *, run_task_id: int, actor: str | None) -> None:
    task = await _load_task_admin(session, run_task_id=run_task_id)
    if task["run_status"] != "expired":
        raise HTTPException(status_code=400, detail="retro-complete is only available on expired runs")
    if task["status"] == "done":
        raise HTTPException(status_code=400, detail="task is already done")
    await session.execute(_SET_TASK_STATUS, {"status": "done", "tid": run_task_id})
    await _log_event(
        session, venue_id=task["venue_id"], run_task_id=run_task_id, employee_id=None,
        event="retro_completed", payload={"by": actor},
    )
    await session.commit()


async def re_resolve_run(session: AsyncSession, *, run_id: int) -> dict:
    """R13: re-resolve a run's audience + refresh visibility snapshots of
    not-yet-started tasks. Does NOT commit (caller's transaction does)."""
    run = (await session.execute(_GET_RUN, {"rid": run_id})).mappings().first()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    d = run["run_date"]
    venue_id = run["venue_id"]
    audience = await resolve_checklist_audience(session, checklist_id=run["checklist_id"], d=d)
    items = await _resolve_items(session, checklist_id=run["checklist_id"])
    vis_by_template = {i["task_template_id"]: i["visible_to_employee_ids"] for i in items}
    for item in items:
        audience |= set(item["visible_to_employee_ids"])

    refreshed = 0
    tasks = (
        await session.execute(
            text("SELECT id, task_template_id, status FROM checklist_run_tasks WHERE run_id = :rid"),
            {"rid": run_id},
        )
    ).mappings().all()
    for t in tasks:
        if t["status"] != "pending":
            continue
        if t["task_template_id"] in vis_by_template:
            await session.execute(
                text("UPDATE checklist_run_tasks SET visible_to_employee_ids = :vis WHERE id = :tid"),
                {"vis": service.json_dump(vis_by_template[t["task_template_id"]]), "tid": t["id"]},
            )
            refreshed += 1

    active = (
        await session.execute(
            text(
                "SELECT DISTINCT p.employee_id FROM checklist_run_task_participants p "
                "JOIN checklist_run_tasks t ON t.id = p.run_task_id WHERE t.run_id = :rid"
            ),
            {"rid": run_id},
        )
    ).mappings().all()
    audience |= {r["employee_id"] for r in active}

    await session.execute(
        text("DELETE FROM checklist_run_assignees WHERE run_id = :rid"), {"rid": run_id}
    )
    assignee_id = await _next_id(session, "checklist_run_assignees")
    for eid in sorted(audience):
        await session.execute(
            _INSERT_RUN_ASSIGNEE, {"id": assignee_id, "v": venue_id, "rid": run_id, "eid": eid}
        )
        assignee_id += 1
    return {"assignee_count": len(audience), "refreshed_tasks": refreshed}


async def re_resolve_open_runs(
    session: AsyncSession, *, checklist_id: int, today: str
) -> int:
    """After assignment changes, refresh the audience of the checklist's already
    generated OPEN runs (today + the operating-day window). Does not commit."""
    min_date = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    rows = (
        await session.execute(
            text(
                "SELECT id FROM checklist_runs WHERE checklist_id = :cid "
                "AND status = 'open' AND run_date >= :min"
            ),
            {"cid": checklist_id, "min": min_date},
        )
    ).mappings().all()
    for r in rows:
        await re_resolve_run(session, run_id=r["id"])
    return len(rows)


async def regenerate_run(session: AsyncSession, *, run_id: int, generated_by: str) -> int | None:
    """R25 cancel-and-regenerate — allowed only while no task has been started."""
    run = (await session.execute(_GET_RUN, {"rid": run_id})).mappings().first()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    activity = (
        await session.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM checklist_run_task_participants p "
                "  JOIN checklist_run_tasks t ON t.id = p.run_task_id WHERE t.run_id = :rid) "
                "OR EXISTS(SELECT 1 FROM checklist_run_events ev "
                "  JOIN checklist_run_tasks t ON t.id = ev.run_task_id WHERE t.run_id = :rid) "
                "AS has_activity"
            ),
            {"rid": run_id},
        )
    ).mappings().first()
    if activity and activity["has_activity"]:
        raise HTTPException(status_code=409, detail="conflict: run already has activity — cannot regenerate")
    checklist = await get_checklist_lean(session, checklist_id=run["checklist_id"])
    if checklist is None:
        raise HTTPException(status_code=404, detail="checklist not found")
    await session.execute(text("DELETE FROM checklist_runs WHERE id = :rid"), {"rid": run_id})
    await session.execute(
        text("DELETE FROM checklist_run_tasks WHERE run_id = :rid"), {"rid": run_id}
    )
    await session.execute(
        text("DELETE FROM checklist_run_assignees WHERE run_id = :rid"), {"rid": run_id}
    )
    new_id = await generate_run(
        session, checklist=dict(checklist), d=run["run_date"], generated_by=generated_by,
        venue_id=run["venue_id"],
    )
    await session.commit()
    return new_id
