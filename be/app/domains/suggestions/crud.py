"""Suggestions CRUD — ``text()`` SQL constants + async functions.

All SQL is dialect-agnostic (sqlite in tests, postgres in prod), mirroring the
hr/tasks/forms domains:

* unqualified table names; surrogate ids allocated app-side (``_next_id``);
* ``is_hidden`` stored as INTEGER 0/1; timestamps as TEXT ISO-8601 (no ``now()``);
* plus-one idempotency is select-then-insert (no ``ON CONFLICT``);
* the comment-image fan-in uses an expanding IN bind (no postgres-only ``ANY(...)``);
* the single-row ``suggestion_settings`` is seeded lazily here (no seed INSERT in
  the migration).

OWNERSHIP: the router resolves the acting employee from the VERIFIED token and
passes that ``employee_id`` in. Visibility (hidden -> author-only) and authorship
(only the author may attach images to their own suggestion/comment) are enforced by
the router using the light head-reads exposed here (:func:`get_head`,
:func:`get_comment_head`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _b(value: Any) -> bool:
    return bool(value)


async def _next_id(session: AsyncSession, table: str) -> int:
    stmt = text(f"SELECT COALESCE(MAX(id), 0) + 1 AS n FROM {table}")  # noqa: S608 - fixed literal
    row = (await session.execute(stmt)).mappings().first()
    return int(row["n"]) if row is not None else 1


# --------------------------------------------------------------------------- #
# Head reads (ownership / visibility guards live in the router)
# --------------------------------------------------------------------------- #

_HEAD = text(
    "SELECT id, employee_id, is_hidden, status FROM suggestions WHERE id = :sid"
)
_COMMENT_HEAD = text(
    "SELECT id, suggestion_id, employee_id FROM suggestion_comments WHERE id = :cid"
)


async def get_head(session: AsyncSession, *, suggestion_id: int) -> dict | None:
    row = (await session.execute(_HEAD, {"sid": suggestion_id})).mappings().first()
    if row is None:
        return None
    return {
        "id": row["id"],
        "employee_id": row["employee_id"],
        "is_hidden": _b(row["is_hidden"]),
        "status": row["status"],
    }


async def get_comment_head(session: AsyncSession, *, comment_id: int) -> dict | None:
    row = (await session.execute(_COMMENT_HEAD, {"cid": comment_id})).mappings().first()
    return dict(row) if row is not None else None


# --------------------------------------------------------------------------- #
# List
# --------------------------------------------------------------------------- #

_LIST = text(
    """
    SELECT s.id, s.title, s.category, s.description, s.status, s.is_hidden,
           s.created_at, s.updated_at, s.employee_id,
           e.name AS author_name, e.surname AS author_surname,
           (SELECT COUNT(*) FROM suggestion_plus_ones p
              WHERE p.suggestion_id = s.id) AS plus_one_count,
           (SELECT COUNT(*) FROM suggestion_comments c
              WHERE c.suggestion_id = s.id) AS comment_count,
           (SELECT 1 FROM suggestion_plus_ones p
              WHERE p.suggestion_id = s.id AND p.employee_id = :viewer_id
              LIMIT 1) AS viewer_plus_oned,
           (SELECT url FROM suggestion_images i
              WHERE i.suggestion_id = s.id
              ORDER BY i.sort_order, i.id LIMIT 1) AS thumbnail_url
    FROM suggestions s
    LEFT JOIN employees e ON e.id = s.employee_id
    WHERE (CAST(:include_hidden AS INTEGER) = 1 OR s.is_hidden = 0)
      AND (CAST(:status_filter AS TEXT) IS NULL
           OR s.status = CAST(:status_filter AS TEXT))
    ORDER BY s.created_at DESC, s.id DESC
    """
)


def _author(row: Any) -> str:
    return " ".join(p for p in (row["author_name"], row["author_surname"]) if p).strip()


async def list_suggestions(
    session: AsyncSession,
    *,
    viewer_id: int,
    include_hidden: bool = False,
    status_filter: str | None = None,
) -> list[dict]:
    rows = (
        await session.execute(
            _LIST,
            {
                "viewer_id": viewer_id,
                "include_hidden": 1 if include_hidden else 0,
                "status_filter": status_filter,
            },
        )
    ).mappings().all()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "category": r["category"],
            "description": r["description"],
            "status": r["status"],
            "is_hidden": _b(r["is_hidden"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "employee_id": r["employee_id"],
            "author": _author(r),
            "plus_one_count": int(r["plus_one_count"]),
            "comment_count": int(r["comment_count"]),
            "viewer_plus_oned": _b(r["viewer_plus_oned"]),
            "thumbnail_url": r["thumbnail_url"],
        }
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# Detail
# --------------------------------------------------------------------------- #

_GET = text(
    """
    SELECT s.id, s.title, s.category, s.description, s.status, s.is_hidden,
           s.hidden_by_email, s.hidden_at, s.closed_by_email, s.closed_at,
           s.created_at, s.updated_at, s.employee_id,
           e.name AS author_name, e.surname AS author_surname,
           (SELECT COUNT(*) FROM suggestion_plus_ones p
              WHERE p.suggestion_id = s.id) AS plus_one_count,
           (SELECT 1 FROM suggestion_plus_ones p
              WHERE p.suggestion_id = s.id AND p.employee_id = :viewer_id
              LIMIT 1) AS viewer_plus_oned
    FROM suggestions s
    LEFT JOIN employees e ON e.id = s.employee_id
    WHERE s.id = :sid
    """
)

_GET_IMAGES = text(
    "SELECT id, url, sort_order FROM suggestion_images "
    "WHERE suggestion_id = :sid ORDER BY sort_order, id"
)

_GET_COMMENTS = text(
    """
    SELECT c.id, c.body, c.created_at, c.employee_id,
           e.name AS author_name, e.surname AS author_surname
    FROM suggestion_comments c
    LEFT JOIN employees e ON e.id = c.employee_id
    WHERE c.suggestion_id = :sid
    ORDER BY c.created_at ASC, c.id ASC
    """
)

_GET_COMMENT_IMAGES = text(
    "SELECT id, comment_id, url, sort_order FROM suggestion_comment_images "
    "WHERE comment_id IN :cids ORDER BY sort_order, id"
).bindparams(bindparam("cids", expanding=True))


async def get_suggestion_detail(
    session: AsyncSession, *, suggestion_id: int, viewer_id: int
) -> dict | None:
    head = (
        await session.execute(_GET, {"sid": suggestion_id, "viewer_id": viewer_id})
    ).mappings().first()
    if head is None:
        return None

    images = [
        {"id": r["id"], "url": r["url"], "sort_order": r["sort_order"]}
        for r in (
            await session.execute(_GET_IMAGES, {"sid": suggestion_id})
        ).mappings().all()
    ]

    comment_rows = (
        await session.execute(_GET_COMMENTS, {"sid": suggestion_id})
    ).mappings().all()
    comments = [
        {
            "id": c["id"],
            "body": c["body"],
            "created_at": c["created_at"],
            "employee_id": c["employee_id"],
            "author": _author(c),
            "images": [],
        }
        for c in comment_rows
    ]

    if comments:
        cids = [c["id"] for c in comments]
        by_cid: dict[int, list[dict]] = {cid: [] for cid in cids}
        for img in (
            await session.execute(_GET_COMMENT_IMAGES, {"cids": cids})
        ).mappings().all():
            by_cid[img["comment_id"]].append({"id": img["id"], "url": img["url"]})
        for c in comments:
            c["images"] = by_cid.get(c["id"], [])

    return {
        "id": head["id"],
        "title": head["title"],
        "category": head["category"],
        "description": head["description"],
        "status": head["status"],
        "is_hidden": _b(head["is_hidden"]),
        "hidden_by_email": head["hidden_by_email"],
        "hidden_at": head["hidden_at"],
        "closed_by_email": head["closed_by_email"],
        "closed_at": head["closed_at"],
        "created_at": head["created_at"],
        "updated_at": head["updated_at"],
        "employee_id": head["employee_id"],
        "author": _author(head),
        "plus_one_count": int(head["plus_one_count"]),
        "viewer_plus_oned": _b(head["viewer_plus_oned"]),
        "images": images,
        "comments": comments,
    }


# --------------------------------------------------------------------------- #
# Create / mutate
# --------------------------------------------------------------------------- #

_INSERT_SUGGESTION = text(
    """
    INSERT INTO suggestions (
        id, venue_id, employee_id, title, category, description,
        status, is_hidden, created_at, updated_at
    ) VALUES (
        :id, :venue_id, :eid, :title, :category, :description,
        'open', 0, :created_at, :created_at
    )
    """
)

_INSERT_SUGGESTION_IMAGE = text(
    "INSERT INTO suggestion_images (id, suggestion_id, url, sort_order, created_at) "
    "VALUES (:id, :sid, :url, :sort_order, :created_at)"
)


async def create_suggestion(
    session: AsyncSession,
    *,
    venue_id: str,
    employee_id: int,
    title: str,
    category: str,
    description: str,
) -> dict:
    new_id = await _next_id(session, "suggestions")
    now = _now_iso()
    await session.execute(
        _INSERT_SUGGESTION,
        {
            "id": new_id,
            "venue_id": venue_id,
            "eid": employee_id,
            "title": title,
            "category": category,
            "description": description,
            "created_at": now,
        },
    )
    await session.commit()
    return {"id": new_id, "created_at": now}


async def attach_suggestion_images(
    session: AsyncSession, *, suggestion_id: int, urls: list[str]
) -> None:
    if not urls:
        return
    next_id = await _next_id(session, "suggestion_images")
    now = _now_iso()
    for idx, url in enumerate(urls):
        await session.execute(
            _INSERT_SUGGESTION_IMAGE,
            {
                "id": next_id,
                "sid": suggestion_id,
                "url": url,
                "sort_order": idx,
                "created_at": now,
            },
        )
        next_id += 1
    await session.commit()


# --------------------------------------------------------------------------- #
# Plus-one (idempotent via select-then-insert)
# --------------------------------------------------------------------------- #

_PLUS_ONE_EXISTS = text(
    "SELECT 1 FROM suggestion_plus_ones "
    "WHERE suggestion_id = :sid AND employee_id = :eid LIMIT 1"
)
_INSERT_PLUS_ONE = text(
    "INSERT INTO suggestion_plus_ones (suggestion_id, employee_id, created_at) "
    "VALUES (:sid, :eid, :created_at)"
)
_DELETE_PLUS_ONE = text(
    "DELETE FROM suggestion_plus_ones WHERE suggestion_id = :sid AND employee_id = :eid"
)
_COUNT_PLUS_ONES = text(
    "SELECT COUNT(*) AS c FROM suggestion_plus_ones WHERE suggestion_id = :sid"
)


async def add_plus_one(
    session: AsyncSession, *, suggestion_id: int, employee_id: int
) -> None:
    exists = (
        await session.execute(
            _PLUS_ONE_EXISTS, {"sid": suggestion_id, "eid": employee_id}
        )
    ).first()
    if not exists:
        await session.execute(
            _INSERT_PLUS_ONE,
            {"sid": suggestion_id, "eid": employee_id, "created_at": _now_iso()},
        )
    await session.commit()


async def remove_plus_one(
    session: AsyncSession, *, suggestion_id: int, employee_id: int
) -> None:
    await session.execute(
        _DELETE_PLUS_ONE, {"sid": suggestion_id, "eid": employee_id}
    )
    await session.commit()


async def count_plus_ones(session: AsyncSession, *, suggestion_id: int) -> int:
    row = (
        await session.execute(_COUNT_PLUS_ONES, {"sid": suggestion_id})
    ).mappings().first()
    return int(row["c"]) if row is not None else 0


# --------------------------------------------------------------------------- #
# Comments
# --------------------------------------------------------------------------- #

_INSERT_COMMENT = text(
    "INSERT INTO suggestion_comments (id, suggestion_id, employee_id, body, created_at) "
    "VALUES (:id, :sid, :eid, :body, :created_at)"
)
_INSERT_COMMENT_IMAGE = text(
    "INSERT INTO suggestion_comment_images (id, comment_id, url, sort_order, created_at) "
    "VALUES (:id, :cid, :url, :sort_order, :created_at)"
)


async def create_comment(
    session: AsyncSession, *, suggestion_id: int, employee_id: int, body: str
) -> dict:
    new_id = await _next_id(session, "suggestion_comments")
    now = _now_iso()
    await session.execute(
        _INSERT_COMMENT,
        {
            "id": new_id,
            "sid": suggestion_id,
            "eid": employee_id,
            "body": body,
            "created_at": now,
        },
    )
    await session.commit()
    return {"id": new_id, "created_at": now}


async def attach_comment_images(
    session: AsyncSession, *, comment_id: int, urls: list[str]
) -> None:
    if not urls:
        return
    next_id = await _next_id(session, "suggestion_comment_images")
    now = _now_iso()
    for idx, url in enumerate(urls):
        await session.execute(
            _INSERT_COMMENT_IMAGE,
            {
                "id": next_id,
                "cid": comment_id,
                "url": url,
                "sort_order": idx,
                "created_at": now,
            },
        )
        next_id += 1
    await session.commit()


# --------------------------------------------------------------------------- #
# Admin: hide / close
# --------------------------------------------------------------------------- #

_SET_HIDDEN = text(
    """
    UPDATE suggestions
    SET is_hidden = :hidden,
        hidden_by_email = :hidden_by,
        hidden_at = :hidden_at,
        updated_at = :now
    WHERE id = :sid
    """
)

_SET_STATUS = text(
    """
    UPDATE suggestions
    SET status = :status,
        closed_by_email = :closed_by,
        closed_at = :closed_at,
        updated_at = :now
    WHERE id = :sid
    """
)


async def set_hidden(
    session: AsyncSession,
    *,
    suggestion_id: int,
    hidden: bool,
    actor_email: str | None,
) -> None:
    now = _now_iso()
    await session.execute(
        _SET_HIDDEN,
        {
            "sid": suggestion_id,
            "hidden": 1 if hidden else 0,
            "hidden_by": actor_email if hidden else None,
            "hidden_at": now if hidden else None,
            "now": now,
        },
    )
    await session.commit()


async def set_status(
    session: AsyncSession,
    *,
    suggestion_id: int,
    status: str,
    actor_email: str | None,
) -> None:
    now = _now_iso()
    closed = status == "closed"
    await session.execute(
        _SET_STATUS,
        {
            "sid": suggestion_id,
            "status": status,
            "closed_by": actor_email if closed else None,
            "closed_at": now if closed else None,
            "now": now,
        },
    )
    await session.commit()


# --------------------------------------------------------------------------- #
# Settings (single row, seeded lazily)
# --------------------------------------------------------------------------- #

_GET_SETTINGS = text(
    "SELECT notification_email, updated_at FROM suggestion_settings WHERE id = 1"
)
_INSERT_SETTINGS = text(
    "INSERT INTO suggestion_settings (id, notification_email, updated_at) "
    "VALUES (1, :email, :now)"
)
_UPDATE_SETTINGS = text(
    "UPDATE suggestion_settings SET notification_email = :email, updated_at = :now "
    "WHERE id = 1"
)


async def get_settings(session: AsyncSession) -> dict:
    row = (await session.execute(_GET_SETTINGS)).mappings().first()
    if row is None:  # lazy single-row seed (no ON CONFLICT in the migration)
        await session.execute(_INSERT_SETTINGS, {"email": "", "now": _now_iso()})
        await session.commit()
        row = (await session.execute(_GET_SETTINGS)).mappings().first()
    assert row is not None  # noqa: S101 - just inserted above
    return {"notification_email": row["notification_email"], "updated_at": row["updated_at"]}


async def update_settings(session: AsyncSession, *, email: str) -> dict:
    await get_settings(session)  # ensure the row exists
    await session.execute(_UPDATE_SETTINGS, {"email": email, "now": _now_iso()})
    await session.commit()
    row = (await session.execute(_GET_SETTINGS)).mappings().first()
    assert row is not None  # noqa: S101 - ensured above
    return {"notification_email": row["notification_email"], "updated_at": row["updated_at"]}


__all__ = [
    "get_head",
    "get_comment_head",
    "list_suggestions",
    "get_suggestion_detail",
    "create_suggestion",
    "attach_suggestion_images",
    "add_plus_one",
    "remove_plus_one",
    "count_plus_ones",
    "create_comment",
    "attach_comment_images",
    "set_hidden",
    "set_status",
    "get_settings",
    "update_settings",
]
