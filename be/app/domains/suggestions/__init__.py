"""Staff suggestions domain — Postgres source-of-truth for a company-wide
idea/bug/improvement board any employee can post to, +1, and comment on; that
admins can hide (per-area visibility) or close; with a single configurable
new-post notification recipient. Builds on the hr domain (employees / admin_users
RBAC) via soft refs.

Layering mirrors the hr/tasks/forms domains: ``schemas`` (pydantic), ``crud``
(``text()`` SQL constants + async fns), ``service`` (pure category/status/image/
email validation), and ``router`` (thin endpoints — staff-self routes resolve the
employee from the VERIFIED token and enforce authorship/visibility, closing the live
query-param identity hole; admin routes are DB-RBAC gated on the ``suggestions``
page, replacing the live bespoke email-spoofable admin check).

DEFERRED (noted, not built here): the actual new-suggestion notification transport
(routed through the Notifier seam — NoopNotifier by default); any bilingual (he/en)
title/body specifics.
"""

from be.app.domains.suggestions.router import router

__all__ = ["router"]
