"""Pydantic request models for the forms / courses / flows / assignments domain.

Create-style models carry sensible defaults (house convention: a builder FE can
``POST {}`` for a draft — templates, courses, flows). Nothing here is
cuisine/brand/locale-specific.

Staff action payloads DELIBERATELY do NOT carry an ``email``/``phone``/``employee_id``
identity: the acting employee is resolved from the VERIFIED token by the router (this
closes the live query-param/body impersonation holes). ``created_by``/``assigned_by``
are likewise resolved from the authenticated admin, never trusted from the body.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# --- forms templates --------------------------------------------------------


class TemplateCreate(BaseModel):
    name_he: str = "Untitled form"
    name_en: str = "Untitled form"
    description_he: str | None = None
    description_en: str | None = None
    fields: list = []
    binding_language: Literal["he", "en", "none"] = "en"
    bilingual: bool = False


class TemplateUpdate(BaseModel):
    name_he: str | None = None
    name_en: str | None = None
    description_he: str | None = None
    description_en: str | None = None
    fields: list | None = None
    binding_language: Literal["he", "en", "none"] | None = None
    bilingual: bool | None = None


class GenerateFromPromptRequest(BaseModel):
    prompt: str = Field("", min_length=0)
    language: Literal["he", "en", "both"] = "en"


class GenerateFromFileRequest(BaseModel):
    file_base64: str = ""
    mime_type: Literal["image/png", "image/jpeg", "image/webp", "application/pdf"] = "application/pdf"
    language: Literal["he", "en", "both"] = "en"
    prompt: str = ""


class TranslateRequest(BaseModel):
    source: Literal["he", "en"] = "en"
    target: Literal["he", "en"] = "he"
    name: str = ""
    fields: list = []


# --- form responses (staff-self; NO identity fields) ------------------------


class StartResponseRequest(BaseModel):
    template_id: str
    assignment_progress_id: str | None = None


class SaveDraftRequest(BaseModel):
    answers: dict = {}


class SubmitNoSigRequest(BaseModel):
    answers: dict = {}


class SignResponseRequest(BaseModel):
    signature_base64: str = ""
    answers: dict = {}
    binding_language_confirmed: str | None = None  # 'he' | 'en' | None


# --- courses ----------------------------------------------------------------


class CourseCreate(BaseModel):
    name_he: str = "Untitled course"
    name_en: str = "Untitled course"
    description_he: str | None = None
    description_en: str | None = None


class CourseUpdate(BaseModel):
    name_he: str | None = None
    name_en: str | None = None
    description_he: str | None = None
    description_en: str | None = None


class SectionCreate(BaseModel):
    title_he: str = ""
    title_en: str = ""
    position: int = 0


class ItemCreate(BaseModel):
    type: Literal["text", "image", "video", "quiz"] = "text"
    payload: dict = {}
    position: int = 0


class CourseItemCompleteRequest(BaseModel):
    assignment_progress_id: str | None = None


# --- flows ------------------------------------------------------------------


class FlowCreate(BaseModel):
    name_he: str = "Untitled flow"
    name_en: str = "Untitled flow"
    ordering: Literal["parallel", "sequential"] = "sequential"
    default_due_days: int | None = None


class FlowUpdate(BaseModel):
    name_he: str | None = None
    name_en: str | None = None
    ordering: Literal["parallel", "sequential"] | None = None
    default_due_days: int | None = None


class FlowItemAdd(BaseModel):
    position: int = 0
    item_type: Literal["form", "course"] = "form"
    item_id: str
    due_days_override: int | None = None


class FlowItemReorder(BaseModel):
    item_ids: list[str] = []


# --- assignments ------------------------------------------------------------


class AssignmentCreate(BaseModel):
    # Single int OR list of ints — the FE composer sends both shapes.
    employee_id: int | list[int]
    source: Literal["flow", "standalone"]
    flow_id: str | None = None
    form_id: str | None = None
    course_id: str | None = None
    due_at: str | None = None  # ISO-8601 timestamp


__all__ = [
    "TemplateCreate",
    "TemplateUpdate",
    "GenerateFromPromptRequest",
    "GenerateFromFileRequest",
    "TranslateRequest",
    "StartResponseRequest",
    "SaveDraftRequest",
    "SubmitNoSigRequest",
    "SignResponseRequest",
    "CourseCreate",
    "CourseUpdate",
    "SectionCreate",
    "ItemCreate",
    "CourseItemCompleteRequest",
    "FlowCreate",
    "FlowUpdate",
    "FlowItemAdd",
    "FlowItemReorder",
    "AssignmentCreate",
]
