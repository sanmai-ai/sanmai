"""Unit tests for the HR service helpers — pure functions, no DB, zero creds.

Covers locale-neutral phone normalization, email normalization, "HH:MM" time
validation/zero-padding, and pay validation.
"""

from __future__ import annotations

import pytest

from be.app.domains.hr import service


def test_normalize_email() -> None:
    assert service.normalize_email("  Foo@Bar.COM ") == "foo@bar.com"
    assert service.normalize_email("   ") is None
    assert service.normalize_email(None) is None


def test_normalize_phone_strips_formatting_and_folds_00() -> None:
    assert service.normalize_phone(" (055) 123-4567 ") == "0551234567"
    assert service.normalize_phone("00441234567890") == "+441234567890"
    assert service.normalize_phone("+15551234567") == "+15551234567"
    assert service.normalize_phone("") is None
    assert service.normalize_phone(None) is None


def test_normalize_phone_never_injects_a_country_code() -> None:
    # Locale-neutral: a bare local number is preserved, not prefixed with a dialing code.
    assert service.normalize_phone("0521234567") == "0521234567"


def test_normalize_phone_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        service.normalize_phone("not-a-phone")
    with pytest.raises(ValueError):
        service.normalize_phone("123")  # too short


def test_normalize_time_pads_and_validates() -> None:
    assert service.normalize_time("9:05") == "09:05"
    assert service.normalize_time("23:59") == "23:59"
    assert service.normalize_time("  ") is None
    assert service.normalize_time(None) is None
    with pytest.raises(ValueError):
        service.normalize_time("nope")
    with pytest.raises(ValueError):
        service.normalize_time("24:00")
    with pytest.raises(ValueError):
        service.normalize_time("10:75")


def test_validate_pay() -> None:
    service.validate_pay("hourly", 30.0)  # no raise
    service.validate_pay("monthly", None)  # no raise
    with pytest.raises(ValueError):
        service.validate_pay("weekly", 10.0)
    with pytest.raises(ValueError):
        service.validate_pay("hourly", -1.0)
