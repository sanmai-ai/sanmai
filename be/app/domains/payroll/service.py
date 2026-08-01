"""Payroll domain service helpers — pure, locale-neutral logic (no DB, no I/O).

The pay math delegates every jurisdiction constant (overtime tiers/multipliers, the
week-start weekday) to the injected :class:`be.adapters.payroll.base.PayrollProfile`;
this module only orchestrates. Dates/times ride the asyncpg-safe TEXT path and are
parsed here. Recurrence expansion for the payment calendar is pure date math and lives
here too. ``split_bonus`` / ``validate_pay`` are reused from the tasks / HR domains.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from be.adapters.payroll.base import PayrollProfile
from be.app.domains.hr.service import validate_pay
from be.app.domains.scheduling.service import day_of_week, parse_date, shift_minutes
from be.app.domains.tasks.service import split_bonus

VALID_PAYMENT_CATEGORIES: tuple[str, ...] = (
    "tax",
    "rent",
    "salary",
    "utility",
    "loan",
    "insurance",
    "subscription",
    "other",
)
VALID_PAYMENT_TYPES: tuple[str, ...] = ("mandatory", "planned", "forecast")


def validate_month(year: int | None, month: int | None) -> tuple[int, int]:
    """Validate a (year, month) pair; raise :class:`ValueError` on bad input."""
    if year is None or month is None:
        raise ValueError("year and month are required")
    if not 1 <= month <= 12:
        raise ValueError("month must be 1..12")
    if year < 1970 or year > 9999:
        raise ValueError("year out of range")
    return year, month


def validate_bonus_amount(amount: float | None) -> float:
    """Validate a bonus amount; raise :class:`ValueError` if missing/negative."""
    if amount is None:
        raise ValueError("amount is required")
    if amount < 0:
        raise ValueError("amount can't be negative")
    return amount


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return parse_date(str(value))


# --- overtime + pay computation --------------------------------------------


def compute_overtime(
    shifts: list[dict], rules: list[dict] | None, profile: PayrollProfile
) -> dict:
    """Split a month's shifts into pay bands via *profile* + apply payment-rule bonuses.

    Returns aggregate minute bands (``regular_minutes`` / ``tier1_minutes`` /
    ``tier2_minutes`` — weekly overtime already folded in), the raw ``bonuses`` list,
    and a per-shift breakdown. Locale-neutral: all tiering comes from *profile*.
    """
    rules = rules or []
    week_start_dow = profile.week_start_dow()
    per_shift: list[dict] = []
    bonuses: list[dict] = []

    for s in shifts:
        minutes = shift_minutes(s.get("start_time"), s.get("end_time")) or 0
        bd = profile.daily_overtime(minutes)
        d = _as_date(s["shift_date"])
        per_shift.append(
            {
                "id": s.get("id"),
                "shift_date": d.isoformat(),
                "total_minutes": minutes,
                "regular_minutes": bd.regular_minutes,
                "tier1_minutes": bd.tier1_minutes,
                "tier2_minutes": bd.tier2_minutes,
                "_dow": day_of_week(d),
                "_date": d,
            }
        )
        for rule in rules:
            if (
                rule.get("employee_id") is not None
                and rule["employee_id"] != s.get("employee_id")
            ):
                continue
            if (
                rule.get("day_of_week") is not None
                and rule["day_of_week"] != day_of_week(d)
            ):
                continue
            after = int(rule.get("after_minutes") or 0)
            if minutes > after:
                bonuses.append(
                    {
                        "rule_id": rule.get("id"),
                        "shift_id": s.get("id"),
                        "bonus_minutes": minutes - after,
                        "bonus_percent": int(rule.get("bonus_percent") or 0),
                        "description": rule.get("description"),
                    }
                )

    # Group by pay-week and compute additional weekly overtime.
    weeks: dict[date, list[dict]] = {}
    for row in per_shift:
        d = row["_date"]
        offset = (row["_dow"] - week_start_dow) % 7
        wk = d - timedelta(days=offset)
        weeks.setdefault(wk, []).append(row)

    weekly_extra_tier1 = 0
    for wk_rows in weeks.values():
        wk_total = sum(r["total_minutes"] for r in wk_rows)
        wk_daily_ot = sum(r["tier1_minutes"] + r["tier2_minutes"] for r in wk_rows)
        weekly_extra_tier1 += profile.weekly_overtime(wk_total, wk_daily_ot)

    daily_regular = sum(r["regular_minutes"] for r in per_shift)
    daily_tier1 = sum(r["tier1_minutes"] for r in per_shift)
    daily_tier2 = sum(r["tier2_minutes"] for r in per_shift)

    for row in per_shift:
        row.pop("_dow", None)
        row.pop("_date", None)

    return {
        "total_minutes": sum(r["total_minutes"] for r in per_shift),
        "regular_minutes": daily_regular - weekly_extra_tier1,
        "tier1_minutes": daily_tier1 + weekly_extra_tier1,
        "tier2_minutes": daily_tier2,
        "weekly_overtime_minutes": weekly_extra_tier1,
        "bonuses": bonuses,
        "shifts": per_shift,
    }


def hourly_rate(
    *,
    pay_type: str,
    pay_rate: float | None,
    monthly_hours: float | None,
    profile: PayrollProfile,
) -> float | None:
    """Derive the effective hourly rate; ``None`` when it can't be computed.

    Hourly employees use ``pay_rate`` directly; monthly employees use
    ``pay_rate / monthly_hours`` (the data-driven divisor), falling back to the
    profile's ``monthly_hours_fallback`` when the month has no configured hours.
    """
    if pay_rate is None:
        return None
    if pay_type != "monthly":
        return pay_rate
    hours = monthly_hours if monthly_hours else profile.monthly_hours_fallback()
    if not hours:
        return None
    return pay_rate / hours


def compute_month_pay(
    *,
    shifts: list[dict],
    rules: list[dict] | None,
    profile: PayrollProfile,
    pay_type: str,
    pay_rate: float | None,
    monthly_hours: float | None,
    one_time_bonuses: list[dict] | None,
) -> dict:
    """Compute an employee's monthly pay via *profile*. Money rounded to 2dp.

    Returns ``computable=False`` (no total) when the hourly rate can't be derived —
    e.g. a monthly employee with no configured hours (matches the live behavior).
    """
    ot = compute_overtime(shifts, rules, profile)
    rate = hourly_rate(
        pay_type=pay_type,
        pay_rate=pay_rate,
        monthly_hours=monthly_hours,
        profile=profile,
    )
    one_time = one_time_bonuses or []
    one_time_total = round(sum(float(b.get("amount") or 0) for b in one_time), 2)

    if rate is None:
        return {
            "computable": False,
            "minutes": ot,
            "one_time_bonus_total": one_time_total,
            "total": None,
        }

    mult = profile.overtime_multipliers()
    regular_pay = ot["regular_minutes"] / 60 * rate * mult.get("regular", 1.0)
    tier1_pay = ot["tier1_minutes"] / 60 * rate * mult.get("tier1", 1.0)
    tier2_pay = ot["tier2_minutes"] / 60 * rate * mult.get("tier2", 1.0)
    rule_bonus_pay = sum(
        b["bonus_minutes"] / 60 * rate * (b["bonus_percent"] / 100)
        for b in ot["bonuses"]
    )
    total = regular_pay + tier1_pay + tier2_pay + rule_bonus_pay + one_time_total

    return {
        "computable": True,
        "hourly_rate": round(rate, 4),
        "regular_pay": round(regular_pay, 2),
        "tier1_pay": round(tier1_pay, 2),
        "tier2_pay": round(tier2_pay, 2),
        "rule_bonus_pay": round(rule_bonus_pay, 2),
        "one_time_bonus_total": one_time_total,
        "minutes": ot,
        "total": round(total, 2),
    }


# --- payment-calendar recurrence expansion ---------------------------------


def adjust_day(year: int, month: int, day: int) -> date:
    """Clamp *day* into *year*/*month*; ``-1`` (or an over-long day) = last day."""
    last = monthrange(year, month)[1]
    if day == -1 or day > last:
        return date(year, month, last)
    return date(year, month, max(1, day))


def expand_recurrence(
    *,
    start_date: date,
    end_date: date | None,
    recurrence: dict | None,
    window_start: date,
    window_end: date,
) -> list[date]:
    """All occurrence dates in ``[window_start, window_end]`` inclusive.

    ``recurrence`` None = one-off on ``start_date``. Supported freqs: ``monthly``
    (``day``; ``-1`` = last day), ``yearly`` (``month``+``day``), ``weekly``
    (``weekday`` 0=Sun..6=Sat). ``interval`` (default 1) walks from ``start_date``.
    """
    if not recurrence:
        return [start_date] if window_start <= start_date <= window_end else []

    freq = recurrence.get("freq")
    interval = max(1, int(recurrence.get("interval") or 1))
    cut_end = min(window_end, end_date) if end_date else window_end
    earliest = max(start_date, window_start)
    out: list[date] = []

    if freq == "monthly":
        day = int(recurrence.get("day", start_date.day))
        y, m = start_date.year, start_date.month
        while True:
            d = adjust_day(y, m, day)
            if d > cut_end:
                break
            if d >= earliest:
                out.append(d)
            m += interval
            while m > 12:
                m -= 12
                y += 1
    elif freq == "yearly":
        month = int(recurrence.get("month", start_date.month))
        day = int(recurrence.get("day", start_date.day))
        y = start_date.year
        while True:
            d = adjust_day(y, month, day)
            if d > cut_end:
                break
            if d >= earliest:
                out.append(d)
            y += interval
    elif freq == "weekly":
        target = int(recurrence.get("weekday", (start_date.weekday() + 1) % 7))
        py_target = (target + 6) % 7  # 0=Sun..6=Sat -> python 0=Mon..6=Sun
        d = start_date + timedelta(days=(py_target - start_date.weekday()) % 7)
        step = timedelta(days=7 * interval)
        while d <= cut_end:
            if d >= earliest:
                out.append(d)
            d += step
    return out


def split_bonus_shares(amount: float, n: int) -> list[float]:
    """Equal 2dp split of *amount* across *n* finishers (remainder to the first)."""
    return [float(s) for s in split_bonus(Decimal(str(amount)), n)]


__all__ = [
    "VALID_PAYMENT_CATEGORIES",
    "VALID_PAYMENT_TYPES",
    "validate_pay",
    "validate_month",
    "validate_bonus_amount",
    "compute_overtime",
    "hourly_rate",
    "compute_month_pay",
    "adjust_day",
    "expand_recurrence",
    "split_bonus_shares",
]
