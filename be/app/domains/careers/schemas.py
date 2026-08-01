"""Pydantic request models for the careers domain.

Create-style models carry sensible defaults (house convention: a builder FE can
``POST {}`` for a draft position). The public :class:`ApplicationCreate` deliberately
carries NO status/created_at — those are server-assigned — and its body is re-validated
server-side by ``service.normalize_application`` (the client checks are not trusted).
"""

from __future__ import annotations

from pydantic import BaseModel


class PositionCreate(BaseModel):
    department: str = "service"  # 'kitchen'|'service'|'bar'|'management'
    work_mode: str = "fulltime"  # 'fulltime'|'parttime'|'shift'
    title_en: str = ""
    title_he: str = ""
    location_en: str = ""
    location_he: str = ""
    salary_en: str = ""
    salary_he: str = ""
    summary_en: str = ""
    summary_he: str = ""
    responsibilities_en: list[str] = []
    responsibilities_he: list[str] = []
    requirements_en: list[str] = []
    requirements_he: list[str] = []
    sort_order: int = 100
    is_active: bool = True


class PositionUpdate(BaseModel):
    department: str | None = None
    work_mode: str | None = None
    title_en: str | None = None
    title_he: str | None = None
    location_en: str | None = None
    location_he: str | None = None
    salary_en: str | None = None
    salary_he: str | None = None
    summary_en: str | None = None
    summary_he: str | None = None
    responsibilities_en: list[str] | None = None
    responsibilities_he: list[str] | None = None
    requirements_en: list[str] | None = None
    requirements_he: list[str] | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class ApplicationCreate(BaseModel):
    position_id: int | None = None
    full_name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    street: str = ""
    experience: str = ""
    start_date: str = ""
    citizenship: bool = False
    english: bool = False
    lang: str = "en"


class ApplicationStatusUpdate(BaseModel):
    status: str = "new"  # 'new'|'reviewed'|'accepted'|'rejected'


__all__ = [
    "PositionCreate",
    "PositionUpdate",
    "ApplicationCreate",
    "ApplicationStatusUpdate",
]
