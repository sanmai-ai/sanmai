"""Unit tests for the tasks/checklists domain service — pure logic, no DB."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from be.app.domains.tasks import service

# --- date / time ------------------------------------------------------------


def test_parse_date_ok_and_bad() -> None:
    assert service.parse_date("2024-01-07") == date(2024, 1, 7)
    with pytest.raises(ValueError):
        service.parse_date("")
    with pytest.raises(ValueError):
        service.parse_date("not-a-date")


def test_parse_hhmm_normalizes_and_rejects() -> None:
    assert service.parse_hhmm("9:05") == "09:05"
    assert service.parse_hhmm("") is None
    assert service.parse_hhmm(None) is None
    with pytest.raises(ValueError):
        service.parse_hhmm("25:00")


def test_day_of_week_sunday_zero() -> None:
    assert service.day_of_week(date(2024, 1, 7)) == 0  # a Sunday
    assert service.day_of_week(date(2024, 1, 8)) == 1  # Monday


# --- validation -------------------------------------------------------------


def test_validators() -> None:
    service.validate_category("opening")
    with pytest.raises(ValueError):
        service.validate_category("bonus")
    service.validate_proof_type("photo")
    with pytest.raises(ValueError):
        service.validate_proof_type("signature")
    service.validate_required_staff(3)
    with pytest.raises(ValueError):
        service.validate_required_staff(0)
    service.validate_recurrence([0, 6])
    with pytest.raises(ValueError):
        service.validate_recurrence([7])


def test_clean_subtasks_trims_and_caps() -> None:
    assert service.clean_subtasks([" a ", "", "b", 5, None]) == ["a", "b"]
    with pytest.raises(ValueError):
        service.clean_subtasks([str(i) for i in range(service.MAX_SUBTASKS + 1)])


# --- json helpers -----------------------------------------------------------


def test_json_load_tolerates_str_and_parsed() -> None:
    assert service.json_load('[1,2]', []) == [1, 2]
    assert service.json_load([1, 2], []) == [1, 2]
    assert service.json_load(None, []) == []
    assert service.json_load("not-json", {"x": 1}) == {"x": 1}


def test_json_dump_roundtrip() -> None:
    assert service.json_load(service.json_dump([1, 2, 3]), []) == [1, 2, 3]


# --- overlap / recurrence ---------------------------------------------------


def test_windows_overlap() -> None:
    # no window = no constraint
    assert service.windows_overlap("09:00", "12:00", None, None) is True
    assert service.windows_overlap("09:00", "12:00", "11:00", "14:00") is True
    assert service.windows_overlap("09:00", "10:00", "11:00", "14:00") is False
    # touching edges do not overlap
    assert service.windows_overlap("09:00", "11:00", "11:00", "14:00") is False


def test_recurrence_includes() -> None:
    sunday = date(2024, 1, 7)
    monday = date(2024, 1, 8)
    assert service.recurrence_includes(None, sunday) is True  # every day
    assert service.recurrence_includes("[]", sunday) is True
    assert service.recurrence_includes("[0]", sunday) is True
    assert service.recurrence_includes("[0]", monday) is False
    assert service.recurrence_includes([1], monday) is True


def test_split_bonus_sums_exactly() -> None:
    shares = service.split_bonus(Decimal("10.00"), 3)
    assert sum(shares) == Decimal("10.00")
    assert shares[0] >= shares[1]  # remainder to the earliest finisher
    assert service.split_bonus(Decimal("9.99"), 0) == []
