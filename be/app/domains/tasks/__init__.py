"""Tasks & checklists domain — Postgres source-of-truth for the reusable task
library (task_templates + dependencies), company-level checklists (items +
assignees) with a manager->admin approval flow, and the per-day, venue-scoped
execution snapshot staff work against (checklist_runs + run_tasks + run_assignees
+ run_task_participants + append-only run_events). Builds on the hr domain
(employees / employee_groups / job_roles) and the scheduling domain (scheduled /
recurring shifts for audience resolution, ``shifts`` for clock-in gating) via soft
refs.

Layering mirrors the hr/menu/inventory/scheduling domains: ``schemas`` (pydantic),
``crud`` (``text()`` SQL constants + async fns), ``service`` (pure date/time/JSON/
overlap/recurrence helpers), and ``router`` (thin endpoints — admin routes DB-RBAC
gated on the ``tasks`` page; staff-self routes resolve the employee from the VERIFIED
token and enforce run/task ownership, closing the live query-param identity holes).

DEFERRED (noted, not built here): bonus crediting (payroll domain — a hook is left in
``crud.finish_task``); the token-guarded run-generation cron endpoint (``generate_pass``
lives in crud); Telegram approval notifications (wire behind a Notifier adapter); a
generic company/venue settings table; the deprecated v1 ``task_instances`` endpoints.
"""

from be.app.domains.tasks.router import router

__all__ = ["router"]
