"""Unit tests for the clock domain service (pure token/TTL/kind helpers, no DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from be.app.domains.clock import service


def test_new_token_is_unique_and_urlsafe() -> None:
    tokens = {service.new_token() for _ in range(200)}
    assert len(tokens) == 200  # no collisions
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    for tok in tokens:
        assert tok  # non-empty
        assert set(tok) <= allowed


def test_valid_kinds_and_statuses() -> None:
    assert service.VALID_KINDS == ("clock_in", "clock_out")
    assert service.CLOCK_IN in service.VALID_KINDS
    assert service.CLOCK_OUT in service.VALID_KINDS
    assert set(service.VALID_STATUSES) == {"pending", "used", "completed", "expired"}


def test_expiry_for_is_ttl_after_created() -> None:
    created = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    expires = service.expiry_for(created)
    assert expires - created == timedelta(seconds=service.SESSION_TTL_SECONDS)


def test_is_expired_boundary_at_ttl() -> None:
    created = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    expires_iso = service.expiry_for(created).isoformat()

    # one second before expiry -> still valid
    just_before = created + timedelta(seconds=service.SESSION_TTL_SECONDS - 1)
    assert service.is_expired(expires_iso, at=just_before) is False

    # exactly at expiry -> expired (<=)
    at_boundary = created + timedelta(seconds=service.SESSION_TTL_SECONDS)
    assert service.is_expired(expires_iso, at=at_boundary) is True

    # after expiry -> expired
    after = created + timedelta(seconds=service.SESSION_TTL_SECONDS + 5)
    assert service.is_expired(expires_iso, at=after) is True


def test_is_expired_treats_malformed_as_expired() -> None:
    assert service.is_expired("not-a-timestamp") is True


def test_is_expired_handles_naive_stored_value() -> None:
    created = datetime(2024, 1, 1, 12, 0, 0)  # naive on purpose
    naive_iso = service.expiry_for(created).isoformat()
    before = datetime(2024, 1, 1, 12, 0, 30, tzinfo=UTC)
    assert service.is_expired(naive_iso, at=before) is False


def test_qr_payload_is_the_token() -> None:
    tok = service.new_token()
    assert service.qr_payload(tok) == tok
