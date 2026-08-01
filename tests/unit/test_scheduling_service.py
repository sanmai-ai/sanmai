"""Unit tests for the scheduling domain's pure service helpers (no DB, no I/O)."""

from __future__ import annotations

from datetime import date

import pytest

from be.app.domains.scheduling import service


def test_sunday_of_week_and_is_sunday() -> None:
    # 2024-01-07 is a Sunday; 2024-01-10 is a Wednesday.
    assert service.is_sunday(date(2024, 1, 7))
    assert not service.is_sunday(date(2024, 1, 10))
    assert service.sunday_of_week(date(2024, 1, 10)) == date(2024, 1, 7)
    assert service.sunday_of_week(date(2024, 1, 7)) == date(2024, 1, 7)


def test_day_of_week_sun_is_zero() -> None:
    assert service.day_of_week(date(2024, 1, 7)) == 0  # Sunday
    assert service.day_of_week(date(2024, 1, 8)) == 1  # Monday
    assert service.day_of_week(date(2024, 1, 13)) == 6  # Saturday


def test_shift_minutes_normal_and_overnight() -> None:
    assert service.shift_minutes("09:00", "17:00") == 480
    # crosses midnight -> +24h wrap (not a negative interval)
    assert service.shift_minutes("22:00", "02:00") == 240
    assert service.shift_minutes("09:00", None) is None
    assert service.shift_minutes(None, "17:00") is None


def test_minutes_to_duration() -> None:
    assert service.minutes_to_duration(480) == "8:00:00"
    assert service.minutes_to_duration(75) == "1:15:00"
    assert service.minutes_to_duration(None) is None


def test_month_bounds() -> None:
    assert service.month_bounds(2024, 1) == ("2024-01-01", "2024-01-31")
    assert service.month_bounds(2024, 2) == ("2024-02-01", "2024-02-29")  # leap
    assert service.month_bounds(2024, 12) == ("2024-12-01", "2024-12-31")


def test_month_is_closed() -> None:
    today = date(2026, 8, 1)
    assert service.month_is_closed(2024, 1, today)  # past -> closed
    assert not service.month_is_closed(2026, 8, today)  # current -> open
    assert not service.month_is_closed(2026, 9, today)  # future -> open


def test_parse_date_and_require_time() -> None:
    assert service.parse_date("2024-01-07") == date(2024, 1, 7)
    assert service.require_time("9:05") == "09:05"
    with pytest.raises(ValueError):
        service.parse_date("")
    with pytest.raises(ValueError):
        service.require_time(None)
    with pytest.raises(ValueError):
        service.require_time("nope")
