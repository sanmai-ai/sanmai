"""Unit tests for the careers domain service — pure logic, no DB."""

from __future__ import annotations

import pytest

from be.app.domains.careers import service


def _good_body(**over: object) -> dict:
    body = {
        "full_name": "Aya Cohen",
        "email": "aya@example.com",
        "phone": "+972 50 123 4567",
        "city": "Tel Aviv",
        "street": "Dizengoff 1",
        "experience": "I have worked in restaurants for several years now.",
        "start_date": "2026-09-01",
        "citizenship": True,
        "english": True,
        "lang": "en",
    }
    body.update(over)
    return body


def test_normalize_department_and_work_mode() -> None:
    assert service.normalize_department("Kitchen") == "kitchen"
    assert service.normalize_department(None) == "service"
    assert service.normalize_work_mode(" PartTime ") == "parttime"
    assert service.normalize_work_mode("") == "fulltime"
    with pytest.raises(ValueError):
        service.normalize_department("marketing")
    with pytest.raises(ValueError):
        service.normalize_work_mode("weekend")


def test_normalize_string_list_from_str_and_list() -> None:
    assert service.normalize_string_list("a\n b \n\nc") == ["a", "b", "c"]
    assert service.normalize_string_list(["x", " y ", ""]) == ["x", "y"]
    assert service.normalize_string_list(None) == []
    with pytest.raises(ValueError):
        service.normalize_string_list(42)


def test_validate_status() -> None:
    assert service.validate_status("Reviewed") == "reviewed"
    with pytest.raises(ValueError):
        service.validate_status("archived")


def test_normalize_email() -> None:
    assert service.normalize_email(" a@b.co ") == "a@b.co"
    with pytest.raises(ValueError):
        service.normalize_email("not-an-email")


def test_normalize_application_ok_trims_and_coerces() -> None:
    out = service.normalize_application(_good_body(full_name="  Aya  ", lang="XX"))
    assert out["full_name"] == "Aya"
    assert out["email"] == "aya@example.com"
    assert out["citizenship"] is True
    assert out["lang"] == "en"  # unknown lang falls back


@pytest.mark.parametrize(
    "over",
    [
        {"full_name": "  "},
        {"email": "bad"},
        {"phone": "12"},  # too few digits
        {"phone": "letters!!"},
        {"city": ""},
        {"street": ""},
        {"start_date": ""},
        {"experience": "too short"},  # < 20 chars
    ],
)
def test_normalize_application_rejects_bad(over: dict) -> None:
    with pytest.raises(ValueError):
        service.normalize_application(_good_body(**over))


def test_safe_filename() -> None:
    assert service.safe_filename("my cv!.pdf") == "my_cv_.pdf"
    assert service.safe_filename(None) == "cv"
    assert len(service.safe_filename("a" * 200)) == 80


def test_rate_limiter_fixed_window() -> None:
    limiter = service.FixedWindowRateLimiter(max_requests=2, window_seconds=100.0)
    assert limiter.allow("k", now=0.0) is True
    assert limiter.allow("k", now=1.0) is True
    assert limiter.allow("k", now=2.0) is False  # budget exhausted
    assert limiter.allow("other", now=2.0) is True  # per-key
    assert limiter.allow("k", now=200.0) is True  # window rolled over
