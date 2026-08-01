"""Pydantic request models for the tasks & checklists domain.

Create-style models carry sensible defaults (house convention: a builder FE can
``POST {}`` for a draft); the router validates the genuinely required fields. Nothing
here is cuisine/brand/locale-specific.

Staff action payloads DELIBERATELY do NOT carry an ``email``/``phone`` identity: the
acting employee is resolved from the VERIFIED token by the router (this closes the
live query-param/body impersonation holes). Only run-execution parameters remain.
"""

from __future__ import annotations

from pydantic import BaseModel

# --- task templates ---------------------------------------------------------


class TemplateCreate(BaseModel):
    title: str = "Untitled task"
    description: str | None = None
    category: str = "during_shift"
    estimated_minutes: int | None = None
    sort_order: int = 0
    is_bonus: bool = False
    bonus_amount: float | None = None
    bonus_currency: str = "ILS"
    recurrence: str | None = None  # v1 leftover string; kept for compatibility
    photos: list[str] = []
    default_proof_type: str = "check"
    required_staff: int = 1
    subtasks: list[str] = []


class TemplateUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    category: str | None = None
    estimated_minutes: int | None = None
    sort_order: int | None = None
    is_active: bool | None = None
    is_bonus: bool | None = None
    bonus_amount: float | None = None
    bonus_currency: str | None = None
    recurrence: str | None = None
    photos: list[str] | None = None
    default_proof_type: str | None = None
    required_staff: int | None = None
    subtasks: list[str] | None = None


class DependencyAdd(BaseModel):
    depends_on_id: int = 0


# --- checklists -------------------------------------------------------------


class ChecklistCreate(BaseModel):
    name: str = "New checklist"
    group_id: int | None = None  # legacy v1 target, still honored
    category: str = "during_shift"
    due_time: str | None = None  # "HH:MM"
    recurrence: list[int] | None = None  # weekday ints 0=Sun..6=Sat; None/[] = every day
    auto_generate: bool = False


class ChecklistUpdate(BaseModel):
    name: str | None = None
    group_id: int | None = None
    category: str | None = None
    is_active: bool | None = None
    due_time: str | None = None  # "HH:MM"; "" clears
    recurrence: list[int] | None = None
    auto_generate: bool | None = None
    updated_at: str | None = None  # stale-save precondition (R11)


class ChecklistItemIn(BaseModel):
    task_template_id: int = 0
    is_mandatory: bool = True  # item-level only, never inherited (D12)
    proof_type: str | None = None  # NULL = inherit template default
    required_staff: int | None = None  # NULL = inherit
    assigned_employee_ids: list[int] = []
    assigned_group_ids: list[int] = []
    assigned_role_ids: list[int] = []  # job_roles.id; resolved via default role


class ChecklistItemsSet(BaseModel):
    template_ids: list[int] | None = None  # legacy v1 shape
    items: list[ChecklistItemIn] | None = None  # v2 shape
    updated_at: str | None = None


# --- assignees --------------------------------------------------------------


class AssigneeIn(BaseModel):
    employee_id: int | None = None
    group_id: int | None = None
    role_id: int | None = None  # job_roles.id
    schedule_scope: str = "anyone"  # 'anyone' | 'scheduled'
    window_start: str | None = None  # "HH:MM"
    window_end: str | None = None


class AssigneesSet(BaseModel):
    assignees: list[AssigneeIn] = []
    updated_at: str | None = None


# --- approval + generation --------------------------------------------------


class RejectPayload(BaseModel):
    note: str = ""


class GeneratePayload(BaseModel):
    date: str | None = None  # "YYYY-MM-DD"; default = today


class QuickAssignNewTask(BaseModel):
    title: str = ""
    description: str | None = None
    estimated_minutes: int | None = None
    default_proof_type: str = "check"
    required_staff: int = 1
    is_bonus: bool = False
    bonus_amount: float | None = None


class QuickAssignPayload(BaseModel):
    task_template_id: int | None = None
    new_task: QuickAssignNewTask | None = None
    employee_ids: list[int] = []
    date: str | None = None
    due_time: str | None = None  # "HH:MM"
    is_mandatory: bool = True


class AiDraftPayload(BaseModel):
    prompt: str = ""
    checklist_id: int | None = None


# --- admin run actions ------------------------------------------------------


class ReleasePayload(BaseModel):
    employee_id: int | None = None


class ReopenPayload(BaseModel):
    employee_id: int | None = None


# --- staff run actions (NO identity fields — resolved from the token) --------


class TakeOverPayload(BaseModel):
    from_employee_id: int | None = None


class SubtaskPayload(BaseModel):
    index: int = 0
    done: bool = True


class FinishPayload(BaseModel):
    note: str | None = None
    photos: list[str] = []  # already-hosted proof urls; multipart upload also supported


__all__ = [
    "TemplateCreate",
    "TemplateUpdate",
    "DependencyAdd",
    "ChecklistCreate",
    "ChecklistUpdate",
    "ChecklistItemIn",
    "ChecklistItemsSet",
    "AssigneeIn",
    "AssigneesSet",
    "RejectPayload",
    "GeneratePayload",
    "QuickAssignNewTask",
    "QuickAssignPayload",
    "AiDraftPayload",
    "ReleasePayload",
    "ReopenPayload",
    "TakeOverPayload",
    "SubtaskPayload",
    "FinishPayload",
]
