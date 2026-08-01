"""Payroll / compensation domain CRUD — ``text()`` SQL constants + async functions.

Covers payment_rules, employee pay-rate writes (on the shared ``employees`` row),
one-time employee_bonuses, the payment calendar (scheduled_payments + overrides +
installments + balances), and the bonus_task_log crediting ledger (the tasks-domain
bonus-hook sink).

All SQL is dialect-agnostic (sqlite in tests, postgres in prod), mirroring
``be.app.domains.hr.crud``:

* unqualified table names; surrogate ids allocated app-side (``MAX(id)+1``);
* booleans stored as INTEGER 0/1; recurrence stored as JSON TEXT, parsed here;
* money bound as text through ``CAST(:x AS numeric)``;
* timestamps written as ISO-8601 strings from Python (no ``now()``);
* upserts are select-then-insert/update (no ``ON CONFLICT``); the bonus-log
  double-pay guard is a select-then-insert over the ``WHERE voided=0`` unique index.

VENUE SCOPING: compensation ties to the employee and is company-level; rows carry
``venue_id`` for uniformity but are not filtered by it. The monthly-salary divisor
(``monthly_working_hours``) and worked ``shifts`` are OWNED by the scheduling domain
and only READ here (via ``scheduling.crud``).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _to_bool(value: Any) -> bool:
    return bool(value)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _num_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _dump_json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _load_json(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


async def _next_id(session: AsyncSession, table: str) -> int:
    stmt = text(f"SELECT COALESCE(MAX(id), 0) + 1 AS n FROM {table}")  # noqa: S608 - fixed literal
    row = (await session.execute(stmt)).mappings().first()
    return int(row["n"]) if row is not None else 1


# --------------------------------------------------------------------------- #
# Payment rules
# --------------------------------------------------------------------------- #

_LIST_RULES = text("""
    SELECT pr.id, pr.employee_id, e.name AS employee_name, e.surname AS employee_surname,
           pr.day_of_week, pr.after_minutes, pr.bonus_percent, pr.description,
           pr.active, pr.created_at
    FROM payment_rules pr
    LEFT JOIN employees e ON e.id = pr.employee_id
    ORDER BY pr.created_at DESC, pr.id DESC
""")

_GET_RULE = text("""
    SELECT id, employee_id, day_of_week, after_minutes, bonus_percent,
           description, active, created_at
    FROM payment_rules WHERE id = :id
""")

_ACTIVE_RULES = text("""
    SELECT id, employee_id, day_of_week, after_minutes, bonus_percent, description
    FROM payment_rules WHERE active = 1
""")

_INSERT_RULE = text("""
    INSERT INTO payment_rules
        (id, venue_id, employee_id, day_of_week, after_minutes, bonus_percent,
         description, active, created_at)
    VALUES
        (:id, :venue_id, :employee_id, :day_of_week, :after_minutes, :bonus_percent,
         :description, :active, :created_at)
""")

_DELETE_RULE = text("DELETE FROM payment_rules WHERE id = :id")


def _row_to_rule(m: RowMapping) -> dict:
    out = {
        "id": m["id"],
        "employee_id": m["employee_id"],
        "day_of_week": m["day_of_week"],
        "after_minutes": m["after_minutes"],
        "bonus_percent": m["bonus_percent"],
        "description": m["description"],
        "active": _to_bool(m["active"]),
        "created_at": m["created_at"],
    }
    if "employee_name" in m.keys():
        out["employee_name"] = m["employee_name"]
        out["employee_surname"] = m["employee_surname"]
    return out


async def list_payment_rules(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(_LIST_RULES)).mappings().all()
    return [_row_to_rule(r) for r in rows]


async def get_payment_rule(session: AsyncSession, *, rule_id: int) -> dict | None:
    row = (await session.execute(_GET_RULE, {"id": rule_id})).mappings().first()
    return _row_to_rule(row) if row is not None else None


async def get_active_payment_rules(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(_ACTIVE_RULES)).mappings().all()
    return [dict(r) for r in rows]


async def create_payment_rule(
    session: AsyncSession,
    *,
    venue_id: str,
    employee_id: int | None,
    day_of_week: int | None,
    after_minutes: int,
    bonus_percent: int,
    description: str | None,
    active: bool,
) -> dict:
    new_id = await _next_id(session, "payment_rules")
    await session.execute(
        _INSERT_RULE,
        {
            "id": new_id,
            "venue_id": venue_id,
            "employee_id": employee_id,
            "day_of_week": day_of_week,
            "after_minutes": after_minutes,
            "bonus_percent": bonus_percent,
            "description": description,
            "active": 1 if active else 0,
            "created_at": _now_iso(),
        },
    )
    await session.commit()
    created = await get_payment_rule(session, rule_id=new_id)
    assert created is not None
    return created


async def update_payment_rule(
    session: AsyncSession, *, rule_id: int, fields: dict[str, Any]
) -> dict | None:
    current = await get_payment_rule(session, rule_id=rule_id)
    if current is None:
        return None
    merged = {
        "employee_id": fields.get("employee_id", current["employee_id"]),
        "day_of_week": fields.get("day_of_week", current["day_of_week"]),
        "after_minutes": (
            fields["after_minutes"]
            if fields.get("after_minutes") is not None
            else current["after_minutes"]
        ),
        "bonus_percent": (
            fields["bonus_percent"]
            if fields.get("bonus_percent") is not None
            else current["bonus_percent"]
        ),
        "description": fields.get("description", current["description"]),
        "active": (
            bool(fields["active"])
            if fields.get("active") is not None
            else current["active"]
        ),
    }
    await session.execute(
        text("""
            UPDATE payment_rules SET
                employee_id = :employee_id, day_of_week = :day_of_week,
                after_minutes = :after_minutes, bonus_percent = :bonus_percent,
                description = :description, active = :active
            WHERE id = :id
        """),
        {"id": rule_id, **merged, "active": 1 if merged["active"] else 0},
    )
    await session.commit()
    return await get_payment_rule(session, rule_id=rule_id)


async def delete_payment_rule(session: AsyncSession, *, rule_id: int) -> bool:
    if await get_payment_rule(session, rule_id=rule_id) is None:
        return False
    await session.execute(_DELETE_RULE, {"id": rule_id})
    await session.commit()
    return True


# --------------------------------------------------------------------------- #
# Employee pay rate (writes the shared employees row)
# --------------------------------------------------------------------------- #

_GET_PAY = text("SELECT id, pay_type, pay_rate FROM employees WHERE id = :id")
_UPDATE_PAY = text(
    "UPDATE employees SET pay_type = :pay_type, pay_rate = CAST(:pay_rate AS numeric), "
    "updated_at = :updated_at WHERE id = :id"
)


async def get_employee_pay(session: AsyncSession, *, employee_id: int) -> dict | None:
    row = (await session.execute(_GET_PAY, {"id": employee_id})).mappings().first()
    if row is None:
        return None
    return {"pay_type": row["pay_type"], "pay_rate": _num(row["pay_rate"])}


async def set_employee_pay(
    session: AsyncSession, *, employee_id: int, pay_type: str, pay_rate: float | None
) -> dict | None:
    if await get_employee_pay(session, employee_id=employee_id) is None:
        return None
    await session.execute(
        _UPDATE_PAY,
        {
            "id": employee_id,
            "pay_type": pay_type,
            "pay_rate": _num_str(pay_rate),
            "updated_at": _now_iso(),
        },
    )
    await session.commit()
    return await get_employee_pay(session, employee_id=employee_id)


# --------------------------------------------------------------------------- #
# One-time bonuses
# --------------------------------------------------------------------------- #

_LIST_BONUSES = text("""
    SELECT b.id, b.employee_id, e.name AS employee_name, e.surname AS employee_surname,
           b.year, b.month, b.amount, b.comment, b.created_at
    FROM employee_bonuses b
    LEFT JOIN employees e ON e.id = b.employee_id
    WHERE b.year = :year AND b.month = :month
    ORDER BY e.name, e.surname, b.id
""")

_GET_BONUS = text("SELECT id, employee_id, year, month, amount, comment FROM employee_bonuses WHERE id = :id")

_EMPLOYEE_BONUSES = text("""
    SELECT id, year, month, amount, comment, created_at
    FROM employee_bonuses
    WHERE employee_id = :eid AND year = :year AND month = :month
    ORDER BY id
""")

_INSERT_BONUS = text("""
    INSERT INTO employee_bonuses
        (id, venue_id, employee_id, year, month, amount, comment, created_at)
    VALUES
        (:id, :venue_id, :employee_id, :year, :month, CAST(:amount AS numeric),
         :comment, :created_at)
""")

_DELETE_BONUS = text("DELETE FROM employee_bonuses WHERE id = :id")


async def list_monthly_bonuses(
    session: AsyncSession, *, year: int, month: int
) -> list[dict]:
    rows = (
        await session.execute(_LIST_BONUSES, {"year": year, "month": month})
    ).mappings().all()
    return [
        {
            "id": r["id"],
            "employee_id": r["employee_id"],
            "employee_name": r["employee_name"],
            "employee_surname": r["employee_surname"],
            "year": r["year"],
            "month": r["month"],
            "amount": _num(r["amount"]),
            "comment": r["comment"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


async def get_employee_bonuses(
    session: AsyncSession, *, employee_id: int, year: int, month: int
) -> list[dict]:
    rows = (
        await session.execute(
            _EMPLOYEE_BONUSES, {"eid": employee_id, "year": year, "month": month}
        )
    ).mappings().all()
    return [
        {
            "id": r["id"],
            "year": r["year"],
            "month": r["month"],
            "amount": _num(r["amount"]),
            "comment": r["comment"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


async def create_bonus(
    session: AsyncSession,
    *,
    venue_id: str,
    employee_id: int,
    year: int,
    month: int,
    amount: float,
    comment: str | None,
) -> dict:
    new_id = await _next_id(session, "employee_bonuses")
    await session.execute(
        _INSERT_BONUS,
        {
            "id": new_id,
            "venue_id": venue_id,
            "employee_id": employee_id,
            "year": year,
            "month": month,
            "amount": _num_str(amount),
            "comment": comment,
            "created_at": _now_iso(),
        },
    )
    await session.commit()
    row = (await session.execute(_GET_BONUS, {"id": new_id})).mappings().first()
    assert row is not None
    return {
        "id": row["id"],
        "employee_id": row["employee_id"],
        "year": row["year"],
        "month": row["month"],
        "amount": _num(row["amount"]),
        "comment": row["comment"],
    }


async def delete_bonus(session: AsyncSession, *, bonus_id: int) -> bool:
    row = (await session.execute(_GET_BONUS, {"id": bonus_id})).first()
    if row is None:
        return False
    await session.execute(_DELETE_BONUS, {"id": bonus_id})
    await session.commit()
    return True


# --------------------------------------------------------------------------- #
# Scheduled payments (payment calendar definitions)
# --------------------------------------------------------------------------- #

_SP_COLS = (
    "id, name, category, payment_type, amount, currency, is_recurring, recurrence, "
    "start_date, end_date, status, is_approximate, link_to_timesheets, notes, "
    "created_by, created_at, updated_at"
)

_LIST_SP = text(
    f"SELECT {_SP_COLS} FROM scheduled_payments "  # noqa: S608 - fixed column literal
    "WHERE (:include_archived = 1 OR status = 'active') ORDER BY start_date, name, id"
)
_GET_SP = text(f"SELECT {_SP_COLS} FROM scheduled_payments WHERE id = :id")  # noqa: S608
_DELETE_SP = text("DELETE FROM scheduled_payments WHERE id = :id")

_INSERT_SP = text("""
    INSERT INTO scheduled_payments
        (id, venue_id, name, category, payment_type, amount, currency, is_recurring,
         recurrence, start_date, end_date, status, is_approximate, link_to_timesheets,
         notes, created_by, created_at, updated_at)
    VALUES
        (:id, :venue_id, :name, :category, :payment_type, CAST(:amount AS numeric),
         :currency, :is_recurring, :recurrence, :start_date, :end_date, 'active',
         :is_approximate, :link_to_timesheets, :notes, :created_by, :created_at,
         :updated_at)
""")


def _row_to_sp(m: RowMapping) -> dict:
    return {
        "id": m["id"],
        "name": m["name"],
        "category": m["category"],
        "payment_type": m["payment_type"],
        "amount": _num(m["amount"]),
        "currency": m["currency"],
        "is_recurring": _to_bool(m["is_recurring"]),
        "recurrence": _load_json(m["recurrence"]),
        "start_date": m["start_date"],
        "end_date": m["end_date"],
        "status": m["status"],
        "is_approximate": _to_bool(m["is_approximate"]),
        "link_to_timesheets": _to_bool(m["link_to_timesheets"]),
        "notes": m["notes"],
        "created_by": m["created_by"],
        "created_at": m["created_at"],
        "updated_at": m["updated_at"],
    }


async def list_scheduled_payments(
    session: AsyncSession, *, include_archived: bool = False
) -> list[dict]:
    rows = (
        await session.execute(
            _LIST_SP, {"include_archived": 1 if include_archived else 0}
        )
    ).mappings().all()
    return [_row_to_sp(r) for r in rows]


async def get_scheduled_payment(
    session: AsyncSession, *, payment_id: int
) -> dict | None:
    row = (await session.execute(_GET_SP, {"id": payment_id})).mappings().first()
    return _row_to_sp(row) if row is not None else None


async def create_scheduled_payment(
    session: AsyncSession, *, venue_id: str, created_by: str | None, payload: dict
) -> dict:
    new_id = await _next_id(session, "scheduled_payments")
    now = _now_iso()
    await session.execute(
        _INSERT_SP,
        {
            "id": new_id,
            "venue_id": venue_id,
            "name": payload["name"],
            "category": payload.get("category") or "other",
            "payment_type": payload.get("payment_type") or "planned",
            "amount": _num_str(payload.get("amount") or 0),
            "currency": payload.get("currency") or "ILS",
            "is_recurring": 1 if payload.get("is_recurring") else 0,
            "recurrence": _dump_json(payload.get("recurrence")),
            "start_date": payload["start_date"],
            "end_date": payload.get("end_date"),
            "is_approximate": 1 if payload.get("is_approximate") else 0,
            "link_to_timesheets": 1 if payload.get("link_to_timesheets") else 0,
            "notes": payload.get("notes"),
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
        },
    )
    await session.commit()
    created = await get_scheduled_payment(session, payment_id=new_id)
    assert created is not None
    return created


async def update_scheduled_payment(
    session: AsyncSession, *, payment_id: int, fields: dict[str, Any]
) -> dict | None:
    current = await get_scheduled_payment(session, payment_id=payment_id)
    if current is None:
        return None
    scalar = {
        "name": current["name"],
        "category": current["category"],
        "payment_type": current["payment_type"],
        "currency": current["currency"],
        "start_date": current["start_date"],
        "end_date": current["end_date"],
        "status": current["status"],
        "notes": current["notes"],
    }
    for k in scalar:
        if fields.get(k) is not None:
            scalar[k] = fields[k]
    amount = fields["amount"] if fields.get("amount") is not None else current["amount"]
    is_recurring = (
        bool(fields["is_recurring"])
        if fields.get("is_recurring") is not None
        else current["is_recurring"]
    )
    is_approximate = (
        bool(fields["is_approximate"])
        if fields.get("is_approximate") is not None
        else current["is_approximate"]
    )
    link_ts = (
        bool(fields["link_to_timesheets"])
        if fields.get("link_to_timesheets") is not None
        else current["link_to_timesheets"]
    )
    recurrence = (
        fields["recurrence"] if "recurrence" in fields else current["recurrence"]
    )
    await session.execute(
        text("""
            UPDATE scheduled_payments SET
                name = :name, category = :category, payment_type = :payment_type,
                amount = CAST(:amount AS numeric), currency = :currency,
                is_recurring = :is_recurring, recurrence = :recurrence,
                start_date = :start_date, end_date = :end_date, status = :status,
                is_approximate = :is_approximate, link_to_timesheets = :link_to_timesheets,
                notes = :notes, updated_at = :updated_at
            WHERE id = :id
        """),
        {
            "id": payment_id,
            **scalar,
            "amount": _num_str(amount),
            "is_recurring": 1 if is_recurring else 0,
            "is_approximate": 1 if is_approximate else 0,
            "link_to_timesheets": 1 if link_ts else 0,
            "recurrence": _dump_json(recurrence),
            "updated_at": _now_iso(),
        },
    )
    await session.commit()
    return await get_scheduled_payment(session, payment_id=payment_id)


async def delete_scheduled_payment(session: AsyncSession, *, payment_id: int) -> bool:
    if await get_scheduled_payment(session, payment_id=payment_id) is None:
        return False
    await session.execute(_DELETE_SP, {"id": payment_id})
    await session.commit()
    return True


# --------------------------------------------------------------------------- #
# Occurrence overrides
# --------------------------------------------------------------------------- #

_GET_OVERRIDE = text(
    "SELECT id FROM payment_occurrence_overrides "
    "WHERE scheduled_payment_id = :sp AND due_date = :due"
)
_OVERRIDES_IN_RANGE = text("""
    SELECT scheduled_payment_id, due_date, status, amount_override,
           paid_at, paid_amount, paid_method, notes
    FROM payment_occurrence_overrides
    WHERE due_date >= :start AND due_date <= :end
""")


async def upsert_override(
    session: AsyncSession, *, venue_id: str, updated_by: str | None, payload: dict
) -> None:
    """Select-then-insert/update a per-(definition, due_date) occurrence override."""
    existing = (
        await session.execute(
            _GET_OVERRIDE,
            {"sp": payload["scheduled_payment_id"], "due": payload["due_date"]},
        )
    ).mappings().first()
    common = {
        "status": payload.get("status", "pending"),
        "amount_override": _num_str(payload.get("amount_override")),
        "paid_at": payload.get("paid_at"),
        "paid_amount": _num_str(payload.get("paid_amount")),
        "paid_method": payload.get("paid_method"),
        "notes": payload.get("notes"),
        "updated_by": updated_by,
        "updated_at": _now_iso(),
    }
    if existing is None:
        new_id = await _next_id(session, "payment_occurrence_overrides")
        await session.execute(
            text("""
                INSERT INTO payment_occurrence_overrides
                    (id, venue_id, scheduled_payment_id, due_date, status,
                     amount_override, paid_at, paid_amount, paid_method, notes,
                     updated_by, updated_at)
                VALUES
                    (:id, :venue_id, :sp, :due, :status, CAST(:amount_override AS numeric),
                     :paid_at, CAST(:paid_amount AS numeric), :paid_method, :notes,
                     :updated_by, :updated_at)
            """),
            {
                "id": new_id,
                "venue_id": venue_id,
                "sp": payload["scheduled_payment_id"],
                "due": payload["due_date"],
                **common,
            },
        )
    else:
        await session.execute(
            text("""
                UPDATE payment_occurrence_overrides SET
                    status = :status, amount_override = CAST(:amount_override AS numeric),
                    paid_at = :paid_at, paid_amount = CAST(:paid_amount AS numeric),
                    paid_method = :paid_method, notes = :notes, updated_by = :updated_by,
                    updated_at = :updated_at
                WHERE id = :id
            """),
            {"id": existing["id"], **common},
        )
    await session.commit()


async def list_overrides_in_range(
    session: AsyncSession, *, start: str, end: str
) -> list[dict]:
    rows = (
        await session.execute(_OVERRIDES_IN_RANGE, {"start": start, "end": end})
    ).mappings().all()
    return [
        {
            "scheduled_payment_id": r["scheduled_payment_id"],
            "due_date": r["due_date"],
            "status": r["status"],
            "amount_override": _num(r["amount_override"]),
            "paid_at": r["paid_at"],
            "paid_amount": _num(r["paid_amount"]),
            "paid_method": r["paid_method"],
            "notes": r["notes"],
        }
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# Installments
# --------------------------------------------------------------------------- #

_LIST_INSTALLMENTS = text("""
    SELECT id, scheduled_payment_id, source_due_date, seq, total_count, due_date,
           amount, status, paid_at, paid_amount, paid_method, notes
    FROM payment_installments
    WHERE scheduled_payment_id = :sp AND source_due_date = :src
    ORDER BY seq
""")
_GET_INSTALLMENT = text(
    "SELECT id, status FROM payment_installments WHERE id = :id"
)
_DELETE_INSTALLMENTS = text(
    "DELETE FROM payment_installments "
    "WHERE scheduled_payment_id = :sp AND source_due_date = :src"
)


async def list_installments(
    session: AsyncSession, *, scheduled_payment_id: int, source_due_date: str
) -> list[dict]:
    rows = (
        await session.execute(
            _LIST_INSTALLMENTS, {"sp": scheduled_payment_id, "src": source_due_date}
        )
    ).mappings().all()
    return [
        {
            "id": r["id"],
            "scheduled_payment_id": r["scheduled_payment_id"],
            "source_due_date": r["source_due_date"],
            "seq": r["seq"],
            "total_count": r["total_count"],
            "due_date": r["due_date"],
            "amount": _num(r["amount"]),
            "status": r["status"],
            "paid_at": r["paid_at"],
            "paid_amount": _num(r["paid_amount"]),
            "paid_method": r["paid_method"],
            "notes": r["notes"],
        }
        for r in rows
    ]


async def create_installment_plan(
    session: AsyncSession,
    *,
    venue_id: str,
    created_by: str | None,
    scheduled_payment_id: int,
    source_due_date: str,
    installments: list[dict],
) -> list[dict]:
    """Replace-all split of one occurrence into 2..4 dated installments."""
    await session.execute(
        _DELETE_INSTALLMENTS, {"sp": scheduled_payment_id, "src": source_due_date}
    )
    now = _now_iso()
    total = len(installments)
    for seq, item in enumerate(installments, start=1):
        new_id = await _next_id(session, "payment_installments")
        await session.execute(
            text("""
                INSERT INTO payment_installments
                    (id, venue_id, scheduled_payment_id, source_due_date, seq,
                     total_count, due_date, amount, status, created_by, created_at,
                     updated_at)
                VALUES
                    (:id, :venue_id, :sp, :src, :seq, :total, :due,
                     CAST(:amount AS numeric), 'pending', :created_by, :created_at,
                     :updated_at)
            """),
            {
                "id": new_id,
                "venue_id": venue_id,
                "sp": scheduled_payment_id,
                "src": source_due_date,
                "seq": seq,
                "total": total,
                "due": item["due_date"],
                "amount": _num_str(item["amount"]),
                "created_by": created_by,
                "created_at": now,
                "updated_at": now,
            },
        )
    await session.commit()
    return await list_installments(
        session,
        scheduled_payment_id=scheduled_payment_id,
        source_due_date=source_due_date,
    )


async def update_installment(
    session: AsyncSession, *, installment_id: int, fields: dict[str, Any]
) -> bool:
    if (await session.execute(_GET_INSTALLMENT, {"id": installment_id})).first() is None:
        return False
    status = fields.get("status", "paid")
    paid_at = _now_iso() if status == "paid" else None
    await session.execute(
        text("""
            UPDATE payment_installments SET
                status = :status, paid_at = :paid_at,
                paid_amount = CAST(:paid_amount AS numeric), paid_method = :paid_method,
                notes = :notes, updated_at = :updated_at
            WHERE id = :id
        """),
        {
            "id": installment_id,
            "status": status,
            "paid_at": paid_at,
            "paid_amount": _num_str(fields.get("paid_amount")),
            "paid_method": fields.get("paid_method"),
            "notes": fields.get("notes"),
            "updated_at": _now_iso(),
        },
    )
    await session.commit()
    return True


# --------------------------------------------------------------------------- #
# Balances (partial-payment remainder)
# --------------------------------------------------------------------------- #

_LIST_BALANCES = text("""
    SELECT id, origin_kind, origin_ref, name, category, payment_type, amount,
           currency, due_date, status, paid_at, paid_amount, paid_method, notes
    FROM payment_balances
    WHERE (:only_pending = 0 OR status = 'pending')
    ORDER BY due_date, id
""")
_GET_BALANCE = text("SELECT id FROM payment_balances WHERE id = :id")


async def list_balances(
    session: AsyncSession, *, only_pending: bool = False
) -> list[dict]:
    rows = (
        await session.execute(_LIST_BALANCES, {"only_pending": 1 if only_pending else 0})
    ).mappings().all()
    return [
        {
            "id": r["id"],
            "origin_kind": r["origin_kind"],
            "origin_ref": r["origin_ref"],
            "name": r["name"],
            "category": r["category"],
            "payment_type": r["payment_type"],
            "amount": _num(r["amount"]),
            "currency": r["currency"],
            "due_date": r["due_date"],
            "status": r["status"],
            "paid_at": r["paid_at"],
            "paid_amount": _num(r["paid_amount"]),
            "paid_method": r["paid_method"],
            "notes": r["notes"],
        }
        for r in rows
    ]


async def create_balance(
    session: AsyncSession,
    *,
    venue_id: str,
    created_by: str | None,
    origin_kind: str,
    origin_ref: str | None,
    name: str,
    category: str | None,
    payment_type: str,
    amount: float,
    currency: str,
    due_date: str,
    notes: str | None,
) -> dict:
    new_id = await _next_id(session, "payment_balances")
    now = _now_iso()
    await session.execute(
        text("""
            INSERT INTO payment_balances
                (id, venue_id, origin_kind, origin_ref, name, category, payment_type,
                 amount, currency, due_date, status, notes, created_by, created_at,
                 updated_at)
            VALUES
                (:id, :venue_id, :origin_kind, :origin_ref, :name, :category,
                 :payment_type, CAST(:amount AS numeric), :currency, :due_date,
                 'pending', :notes, :created_by, :created_at, :updated_at)
        """),
        {
            "id": new_id,
            "venue_id": venue_id,
            "origin_kind": origin_kind,
            "origin_ref": origin_ref,
            "name": name,
            "category": category,
            "payment_type": payment_type,
            "amount": _num_str(amount),
            "currency": currency,
            "due_date": due_date,
            "notes": notes,
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
        },
    )
    await session.commit()
    rows = await list_balances(session)
    return next(b for b in rows if b["id"] == new_id)


async def update_balance(
    session: AsyncSession, *, balance_id: int, fields: dict[str, Any]
) -> bool:
    if (await session.execute(_GET_BALANCE, {"id": balance_id})).first() is None:
        return False
    status = fields.get("status", "paid")
    paid_at = _now_iso() if status == "paid" else None
    await session.execute(
        text("""
            UPDATE payment_balances SET
                status = :status, paid_at = :paid_at,
                paid_amount = CAST(:paid_amount AS numeric), paid_method = :paid_method,
                notes = :notes, updated_at = :updated_at
            WHERE id = :id
        """),
        {
            "id": balance_id,
            "status": status,
            "paid_at": paid_at,
            "paid_amount": _num_str(fields.get("paid_amount")),
            "paid_method": fields.get("paid_method"),
            "notes": fields.get("notes"),
            "updated_at": _now_iso(),
        },
    )
    await session.commit()
    return True


# --------------------------------------------------------------------------- #
# Bonus task log (the tasks-domain bonus-hook sink)
# --------------------------------------------------------------------------- #

_LIVE_BONUS_FOR = text(
    "SELECT id FROM bonus_task_log "
    "WHERE run_task_id = :rt AND employee_id = :eid AND voided = 0"
)
_LIST_BONUS_LOG = text("""
    SELECT id, run_task_id, employee_id, task_title, bonus_amount, bonus_currency,
           completed_at, paid, paid_at, voided
    FROM bonus_task_log
    WHERE (:only_unpaid = 0 OR (paid = 0 AND voided = 0))
    ORDER BY completed_at DESC, id DESC
""")
_EMPLOYEE_BONUS_LOG = text("""
    SELECT id, run_task_id, task_title, bonus_amount, bonus_currency,
           completed_at, paid, paid_at, voided
    FROM bonus_task_log
    WHERE employee_id = :eid AND voided = 0
    ORDER BY completed_at DESC, id DESC
""")
_GET_BONUS_LOG = text("SELECT id, paid FROM bonus_task_log WHERE id = :id")
_MARK_PAID = text(
    "UPDATE bonus_task_log SET paid = 1, paid_at = :paid_at WHERE id = :id"
)
_VOID_LIVE = text(
    "UPDATE bonus_task_log SET voided = 1 "
    "WHERE run_task_id = :rt AND employee_id = :eid AND voided = 0"
)


def _row_to_bonus_log(m: RowMapping) -> dict:
    out = {
        "id": m["id"],
        "run_task_id": m["run_task_id"],
        "task_title": m["task_title"],
        "bonus_amount": _num(m["bonus_amount"]),
        "bonus_currency": m["bonus_currency"],
        "completed_at": m["completed_at"],
        "paid": _to_bool(m["paid"]),
        "paid_at": m["paid_at"],
        "voided": _to_bool(m["voided"]),
    }
    if "employee_id" in m.keys():
        out["employee_id"] = m["employee_id"]
    return out


async def credit_task_bonus(
    session: AsyncSession,
    *,
    venue_id: str,
    run_task_id: int,
    employee_id: int,
    amount: float,
    currency: str = "ILS",
    task_title: str | None = None,
    commit: bool = True,
) -> dict | None:
    """Credit one finisher's bonus into ``bonus_task_log`` — idempotent.

    Returns the new row, or ``None`` if a LIVE (non-voided) row already exists for
    ``(run_task_id, employee_id)`` (the double-pay guard). Callable inside another
    transaction with ``commit=False`` (the tasks hook shares the finish_task txn).
    """
    dup = (
        await session.execute(
            _LIVE_BONUS_FOR, {"rt": run_task_id, "eid": employee_id}
        )
    ).first()
    if dup is not None:
        return None
    new_id = await _next_id(session, "bonus_task_log")
    await session.execute(
        text("""
            INSERT INTO bonus_task_log
                (id, venue_id, run_task_id, task_instance_id, employee_id, task_title,
                 bonus_amount, bonus_currency, completed_at, paid, voided)
            VALUES
                (:id, :venue_id, :rt, NULL, :eid, :title,
                 CAST(:amount AS numeric), :currency, :completed_at, 0, 0)
        """),
        {
            "id": new_id,
            "venue_id": venue_id,
            "rt": run_task_id,
            "eid": employee_id,
            "title": task_title,
            "amount": _num_str(amount),
            "currency": currency,
            "completed_at": _now_iso(),
        },
    )
    if commit:
        await session.commit()
    return {
        "id": new_id,
        "run_task_id": run_task_id,
        "employee_id": employee_id,
        "bonus_amount": amount,
        "bonus_currency": currency,
    }


async def void_task_bonus(
    session: AsyncSession, *, run_task_id: int, employee_id: int, commit: bool = True
) -> int:
    """Void the LIVE payout(s) for ``(run_task_id, employee_id)`` (task reopened).

    Voided rows survive for audit; a later re-credit inserts fresh live rows.
    Returns the number of rows voided.
    """
    result = await session.execute(
        _VOID_LIVE, {"rt": run_task_id, "eid": employee_id}
    )
    if commit:
        await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


async def list_bonus_task_log(
    session: AsyncSession, *, only_unpaid: bool = False
) -> list[dict]:
    rows = (
        await session.execute(_LIST_BONUS_LOG, {"only_unpaid": 1 if only_unpaid else 0})
    ).mappings().all()
    return [_row_to_bonus_log(r) for r in rows]


async def list_employee_bonus_task_log(
    session: AsyncSession, *, employee_id: int
) -> list[dict]:
    rows = (
        await session.execute(_EMPLOYEE_BONUS_LOG, {"eid": employee_id})
    ).mappings().all()
    return [_row_to_bonus_log(r) for r in rows]


async def mark_bonus_paid(session: AsyncSession, *, log_id: int) -> bool:
    if (await session.execute(_GET_BONUS_LOG, {"id": log_id})).first() is None:
        return False
    await session.execute(_MARK_PAID, {"id": log_id, "paid_at": _now_iso()})
    await session.commit()
    return True


# --------------------------------------------------------------------------- #
# Calendar build (locale-neutral; supplier-derived rows DEFERRED)
# --------------------------------------------------------------------------- #


async def build_calendar(
    session: AsyncSession, *, window_start: str, window_end: str
) -> list[dict]:
    """Expand scheduled_payments over [window_start, window_end] + apply overrides.

    Supplier "known payments" (derived from inventory purchase orders) are DEFERRED
    — that is a cross-domain read to wire later. Installment/balance emission is left
    to their own list endpoints in this increment.
    """
    from be.app.domains.payroll import service

    ws = date.fromisoformat(window_start)
    we = date.fromisoformat(window_end)
    defs = await list_scheduled_payments(session, include_archived=False)
    overrides = await list_overrides_in_range(
        session, start=window_start, end=window_end
    )
    ov_map = {
        (o["scheduled_payment_id"], o["due_date"]): o for o in overrides
    }

    out: list[dict] = []
    for sp in defs:
        occurrences = service.expand_recurrence(
            start_date=date.fromisoformat(sp["start_date"]),
            end_date=date.fromisoformat(sp["end_date"]) if sp["end_date"] else None,
            recurrence=sp["recurrence"] if sp["is_recurring"] else None,
            window_start=ws,
            window_end=we,
        )
        for occ in occurrences:
            iso = occ.isoformat()
            ov = ov_map.get((sp["id"], iso))
            amount = sp["amount"]
            status = "pending"
            if ov is not None:
                status = ov["status"]
                if ov["amount_override"] is not None:
                    amount = ov["amount_override"]
            out.append(
                {
                    "scheduled_payment_id": sp["id"],
                    "name": sp["name"],
                    "category": sp["category"],
                    "payment_type": sp["payment_type"],
                    "currency": sp["currency"],
                    "due_date": iso,
                    "amount": amount,
                    "status": status,
                    "is_approximate": sp["is_approximate"],
                }
            )
    out.sort(key=lambda r: (r["due_date"], r["name"]))
    return out


__all__ = [
    "list_payment_rules",
    "get_payment_rule",
    "get_active_payment_rules",
    "create_payment_rule",
    "update_payment_rule",
    "delete_payment_rule",
    "get_employee_pay",
    "set_employee_pay",
    "list_monthly_bonuses",
    "get_employee_bonuses",
    "create_bonus",
    "delete_bonus",
    "list_scheduled_payments",
    "get_scheduled_payment",
    "create_scheduled_payment",
    "update_scheduled_payment",
    "delete_scheduled_payment",
    "upsert_override",
    "list_overrides_in_range",
    "list_installments",
    "create_installment_plan",
    "update_installment",
    "list_balances",
    "create_balance",
    "update_balance",
    "credit_task_bonus",
    "void_task_bonus",
    "list_bonus_task_log",
    "list_employee_bonus_task_log",
    "mark_bonus_paid",
    "build_calendar",
]
