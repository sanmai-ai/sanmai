"""Careers domain — Postgres source-of-truth for public job POSITIONS and the
APPLICATIONS the public submits against them (optionally with a CV file), which admins
review and triage.

REPLACES prod Firestore (`careers_positions` / `careers_applications`, guarded by
Firestore rules) with portable Postgres tables so authorization, validation, and PII
handling move server-side. NET-NEW vs live: CV/resume upload+download through the
Storage seam (the live apply form is text-only).

Layering mirrors the suggestions domain: ``schemas`` (pydantic), ``crud`` (``text()``
SQL constants + async fns), ``service`` (pure department/work-mode/status validation,
public-apply body validation, CV caps, and an in-process rate-limit guard), and
``router`` (thin endpoints — a PUBLIC unauthenticated apply/listing surface that is
rate-limited, validated, and PII-free, plus admin position management + application
review + CV download gated on the ``careers`` page via DB-RBAC).

DEFERRED (noted, not built here): the new-application notification transport (Notifier
seam — NoopNotifier by default); a distributed/production rate-limiter backend; any
bilingual (he/en) notification subject/body specifics.
"""

from be.app.domains.careers.router import router

__all__ = ["router"]
