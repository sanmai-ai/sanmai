"""HR & access domain — Postgres source-of-truth for employees, the admin_users
RBAC trust anchor (+ per-venue grants), job roles, employee groups + membership,
and manager -> employee assignments.

Layering mirrors the menu/inventory domains: ``schemas`` (pydantic), ``crud``
(``text()`` SQL constants + async fns), ``service`` (pure identity/validation
helpers), and ``router`` (thin, DB-RBAC-gated endpoints). There is no document-store
read copy, so this domain has no projection seam.
"""

from be.app.domains.hr.router import router

__all__ = ["router"]
