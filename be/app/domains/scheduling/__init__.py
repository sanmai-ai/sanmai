"""Scheduling domain — Postgres source-of-truth for weekly schedules, per-day and
recurring shifts, shift-change requests, employee availability, worked-shift/timesheet
records + confirmations + corrections, time-off requests, and monthly working-hours
config. Builds on the hr domain (employees / admin_users / job_roles) via soft refs.

Layering mirrors the hr/menu/inventory domains: ``schemas`` (pydantic), ``crud``
(``text()`` SQL constants + async fns), ``service`` (pure date/time/ownership helpers),
and ``router`` (thin endpoints — admin routes DB-RBAC-gated, staff-self routes resolve
the employee from the VERIFIED token and enforce ownership).

DEFERRED (noted, not built here): payroll rules + overtime engine, the Firestore
clock-in/out QR flow that writes ``shifts`` in prod, and Telegram approval
notifications (wire later behind a Notifier adapter).
"""

from be.app.domains.scheduling.router import router

__all__ = ["router"]
