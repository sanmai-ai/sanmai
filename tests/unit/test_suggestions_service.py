"""Unit tests for the suggestions domain service — pure logic, no DB."""

from __future__ import annotations

import pytest

from be.app.domains.suggestions import service


def test_normalize_category_ok_and_default() -> None:
    assert service.normalize_category("Kitchen") == "kitchen"
    assert service.normalize_category("  FOH ") == "foh"
    assert service.normalize_category(None) == "other"
    assert service.normalize_category("") == "other"


def test_normalize_category_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        service.normalize_category("marketing")


def test_normalize_title_trims_defaults_and_caps() -> None:
    assert service.normalize_title("  Great idea  ") == "Great idea"
    assert service.normalize_title("") == "Untitled suggestion"
    assert service.normalize_title(None) == "Untitled suggestion"
    with pytest.raises(ValueError):
        service.normalize_title("x" * (service.MAX_TITLE_LEN + 1))


def test_validate_status() -> None:
    assert service.validate_status("open") == "open"
    assert service.validate_status("CLOSED") == "closed"
    with pytest.raises(ValueError):
        service.validate_status("archived")


def test_validate_email() -> None:
    assert service.validate_email(" a@b.co ") == "a@b.co"
    with pytest.raises(ValueError):
        service.validate_email("not-an-email")
    with pytest.raises(ValueError):
        service.validate_email("a@" + "x" * service.MAX_EMAIL_LEN)


def test_safe_filename_sanitises_and_caps() -> None:
    assert service.safe_filename("my photo!.png") == "my_photo_.png"
    assert service.safe_filename(None) == "upload"
    assert service.safe_filename("") == "upload"
    assert len(service.safe_filename("a" * 200)) == 80
