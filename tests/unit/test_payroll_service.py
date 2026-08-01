"""Unit tests for the payroll domain service + the PayrollProfile seam (pure, no DB).

Covers a generic-profile pay computation (the locale-neutral default), the same math
with configurable overtime bands, the Israeli profile's 480/600/2520 boundaries kept
pinned without leaking into the generic path, monthly-employee divisor handling,
bonus splitting, and payment-calendar recurrence expansion.
"""

from __future__ import annotations

from datetime import date

from be.adapters.payroll.generic import GenericPayrollProfile
from be.adapters.payroll.il import IsraeliPayrollProfile
from be.app.domains.payroll import service


def _shift(d: str, start: str, end: str, emp: int = 1) -> dict:
    return {"id": 1, "employee_id": emp, "shift_date": d, "start_time": start, "end_time": end}


# --- generic profile (default = no overtime tiering) ------------------------


def test_generic_default_is_flat_pay() -> None:
    profile = GenericPayrollProfile()
    # One 8h shift, hourly @ 40 -> everything regular, total 320.
    result = service.compute_month_pay(
        shifts=[_shift("2026-01-05", "09:00", "17:00")],
        rules=[],
        profile=profile,
        pay_type="hourly",
        pay_rate=40.0,
        monthly_hours=None,
        one_time_bonuses=None,
    )
    assert result["computable"] is True
    assert result["minutes"]["regular_minutes"] == 480
    assert result["minutes"]["tier1_minutes"] == 0
    assert result["total"] == 320.0


def test_generic_configurable_overtime_bands() -> None:
    profile = GenericPayrollProfile(
        daily_regular_cap=480,
        daily_tier1_cap=120,
        weekly_threshold=2520,
        tier1_multiplier=1.25,
        tier2_multiplier=1.5,
    )
    # One 11h shift (660 min) @ 60/hr: 480 reg + 120 t1 + 60 t2.
    result = service.compute_month_pay(
        shifts=[_shift("2026-01-05", "08:00", "19:00")],
        rules=[],
        profile=profile,
        pay_type="hourly",
        pay_rate=60.0,
        monthly_hours=None,
        one_time_bonuses=None,
    )
    m = result["minutes"]
    assert (m["regular_minutes"], m["tier1_minutes"], m["tier2_minutes"]) == (480, 120, 60)
    # 480 + (120/60*60*1.25=150) + (60/60*60*1.5=90) = 720
    assert result["regular_pay"] == 480.0
    assert result["tier1_pay"] == 150.0
    assert result["tier2_pay"] == 90.0
    assert result["total"] == 720.0


def test_one_time_bonus_added_to_total() -> None:
    profile = GenericPayrollProfile()
    result = service.compute_month_pay(
        shifts=[_shift("2026-01-05", "09:00", "17:00")],
        rules=[],
        profile=profile,
        pay_type="hourly",
        pay_rate=40.0,
        monthly_hours=None,
        one_time_bonuses=[{"amount": 100.0}, {"amount": 50.0}],
    )
    assert result["one_time_bonus_total"] == 150.0
    assert result["total"] == 470.0


def test_monthly_employee_without_hours_is_not_computable() -> None:
    profile = GenericPayrollProfile()  # no monthly_hours_fallback
    result = service.compute_month_pay(
        shifts=[_shift("2026-01-05", "09:00", "17:00")],
        rules=[],
        profile=profile,
        pay_type="monthly",
        pay_rate=8000.0,
        monthly_hours=None,
        one_time_bonuses=None,
    )
    assert result["computable"] is False
    assert result["total"] is None


def test_monthly_employee_divisor_from_hours() -> None:
    profile = GenericPayrollProfile()
    rate = service.hourly_rate(
        pay_type="monthly", pay_rate=8000.0, monthly_hours=200.0, profile=profile
    )
    assert rate == 40.0


def test_payment_rule_bonus_applies() -> None:
    profile = GenericPayrollProfile()
    rules = [
        {
            "id": 7,
            "employee_id": 1,
            "day_of_week": None,
            "after_minutes": 360,  # bonus on minutes past 6h
            "bonus_percent": 10,
        }
    ]
    result = service.compute_month_pay(
        shifts=[_shift("2026-01-05", "09:00", "17:00")],  # 480 min
        rules=rules,
        profile=profile,
        pay_type="hourly",
        pay_rate=60.0,
        monthly_hours=None,
        one_time_bonuses=None,
    )
    # bonus_minutes = 480-360 = 120; pay = 120/60*60*0.10 = 12
    assert result["rule_bonus_pay"] == 12.0
    assert result["total"] == 480.0 + 12.0


# --- Israeli profile boundaries (pinned; no IL constants in the generic path) ---


def test_il_profile_daily_boundaries() -> None:
    il = IsraeliPayrollProfile()
    assert il.daily_overtime(480) == il.daily_overtime(480)
    b8 = il.daily_overtime(480)
    assert (b8.regular_minutes, b8.tier1_minutes, b8.tier2_minutes) == (480, 0, 0)
    b10 = il.daily_overtime(600)
    assert (b10.regular_minutes, b10.tier1_minutes, b10.tier2_minutes) == (480, 120, 0)
    b11 = il.daily_overtime(660)
    assert (b11.regular_minutes, b11.tier1_minutes, b11.tier2_minutes) == (480, 120, 60)
    assert il.overtime_multipliers() == {"regular": 1.0, "tier1": 1.25, "tier2": 1.5}
    assert il.week_start_dow() == 0


def test_il_profile_weekly_threshold() -> None:
    il = IsraeliPayrollProfile()
    assert il.weekly_overtime(2520, 0) == 0  # exactly 42h
    assert il.weekly_overtime(3000, 0) == 480  # 8h over, none counted daily
    assert il.weekly_overtime(3000, 200) == 280  # 200 already counted daily


def test_generic_default_has_no_weekly_overtime() -> None:
    generic = GenericPayrollProfile()
    assert generic.weekly_overtime(999999, 0) == 0
    assert generic.overtime_multipliers()["tier1"] == 1.0


# --- helpers ---------------------------------------------------------------


def test_split_bonus_shares_sum_exactly() -> None:
    shares = service.split_bonus_shares(100.0, 3)
    assert shares == [33.34, 33.33, 33.33]
    assert round(sum(shares), 2) == 100.0


def test_expand_recurrence_monthly() -> None:
    out = service.expand_recurrence(
        start_date=date(2026, 1, 15),
        end_date=None,
        recurrence={"freq": "monthly", "day": 15},
        window_start=date(2026, 1, 1),
        window_end=date(2026, 3, 31),
    )
    assert out == [date(2026, 1, 15), date(2026, 2, 15), date(2026, 3, 15)]


def test_expand_recurrence_monthly_last_day_and_interval() -> None:
    out = service.expand_recurrence(
        start_date=date(2026, 1, 31),
        end_date=None,
        recurrence={"freq": "monthly", "interval": 2, "day": -1},
        window_start=date(2026, 1, 1),
        window_end=date(2026, 5, 31),
    )
    # Jan (anchor), Mar, May — last day each, Feb skipped by interval=2.
    assert out == [date(2026, 1, 31), date(2026, 3, 31), date(2026, 5, 31)]


def test_expand_recurrence_one_off() -> None:
    assert service.expand_recurrence(
        start_date=date(2026, 2, 10),
        end_date=None,
        recurrence=None,
        window_start=date(2026, 1, 1),
        window_end=date(2026, 3, 1),
    ) == [date(2026, 2, 10)]
    # outside window -> nothing
    assert service.expand_recurrence(
        start_date=date(2026, 6, 10),
        end_date=None,
        recurrence=None,
        window_start=date(2026, 1, 1),
        window_end=date(2026, 3, 1),
    ) == []


def test_validate_month_and_bonus() -> None:
    assert service.validate_month(2026, 7) == (2026, 7)
    for bad in [(2026, 0), (2026, 13), (None, 5)]:
        try:
            service.validate_month(*bad)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"expected ValueError for {bad}")
