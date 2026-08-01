"""Payroll & compensation domain — employee-pay money (NOT the customer POS path).

Postgres source-of-truth for per-employee/day payment rules, one-time bonuses, the
plan-side payment calendar (scheduled payments + occurrence overrides + installments +
partial-payment balances), and the ``bonus_task_log`` crediting ledger that the
tasks-domain bonus hook writes into.

Layering mirrors the hr/scheduling domains: ``schemas`` (pydantic), ``crud`` (``text()``
SQL constants + async fns), ``service`` (pure, locale-neutral pay math + recurrence),
and ``router`` (thin, DB-RBAC-gated endpoints). The jurisdiction pay math (overtime
tiers/multipliers, week start) lives behind the ``PayrollProfile`` seam
(:mod:`be.adapters.payroll`), selected by ``SANMAI_PAYROLL_PROFILE`` — the domain core
is locale-neutral.

AUTH: every write requires ``require_admin("payroll")`` — this closes the legacy
UNAUTHENTICATED pay-rate / bonus / payment-rule / calendar writes. Employees self-read
only their OWN pay summary + bonus log, resolved from the verified token identity.

SHARED, NOT OWNED HERE: ``employees.pay_type`` / ``employees.pay_rate`` (HR domain,
0004), and ``monthly_working_hours`` (the monthly-salary divisor) + ``shifts`` (worked
hours), both owned by the scheduling domain (0005) and only READ here. The scheduling
domain already exposes gated working-hours config endpoints; payroll does not duplicate
them.

DEFERRED: Telegram approval on scheduled payments (Notifier seam); accountant
export/email (Notifier + Storage seams); the nightly auto-close scheduler job;
supplier-derived calendar rows (a cross-domain inventory read); exact IL rounding edge
cases beyond the profile.
"""

from be.app.domains.payroll.router import router

__all__ = ["router"]
