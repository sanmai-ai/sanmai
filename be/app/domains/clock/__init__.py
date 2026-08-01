"""Clock domain — staff clock-in/out via a short-TTL QR session, reimplemented as a
portable Postgres store (no Firestore). A station operator mints a session (QR); the
staff phone redeems it against the VERIFIED token to clock in/out, which WRITES a
``shifts`` row (the scheduling domain's worked-shift table, ``source='clock'``) — the
seam explicitly deferred by 0005_scheduling.sql.

Layering mirrors the scheduling/hr domains: ``schemas`` (pydantic), ``crud``
(``text()`` SQL constants + async fns), ``service`` (pure token/TTL helpers), and
``router`` (thin endpoints — station routes DB-RBAC-gated on the ``station`` page,
the staff redeem resolves the employee from the verified token and enforces
single-use / TTL / venue binding).

DEFERRED (noted, not built): realtime push (poll instead of Firestore listeners),
physical kiosk-device specifics, and Telegram clock notifications.
"""

from be.app.domains.clock.router import router

__all__ = ["router"]
