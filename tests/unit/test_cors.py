"""Unit tests for the config-driven CORS seam in :func:`be.app.main.create_app`.

CORS is opt-in: with no ``SANMAI_CORS_ORIGINS``/``SANMAI_CORS_ORIGIN_REGEX`` set no
middleware is mounted (a preflight gets no ``Access-Control-Allow-Origin``); with an
Origin configured, a browser preflight is answered with the CORS headers so the static
FE can call the API cross-origin. ``get_settings`` is lru-cached, so each case clears
the cache after patching the environment.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_settings_cache():  # type: ignore[no-untyped-def]
    """Drop the process-wide settings cache before and after each test."""
    from be.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_preflight_allows_configured_exact_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    origin = "https://sanmai-app-demo.pages.dev"
    monkeypatch.setenv("SANMAI_CORS_ORIGINS", f"{origin},http://localhost:8000")

    from be.app.main import create_app

    client = TestClient(create_app())
    resp = client.options(
        "/api/v1/menu/items",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert resp.headers.get("access-control-allow-origin") == origin
    allowed_methods = resp.headers.get("access-control-allow-methods", "")
    assert "GET" in allowed_methods
    assert "POST" in allowed_methods


def test_preflight_allows_origin_regex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANMAI_CORS_ORIGIN_REGEX", r"https://.*\.pages\.dev")

    from be.app.main import create_app

    client = TestClient(create_app())
    origin = "https://preview-123.pages.dev"
    resp = client.options(
        "/api/v1/menu/items",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )

    assert resp.headers.get("access-control-allow-origin") == origin


def test_no_cors_headers_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANMAI_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("SANMAI_CORS_ORIGIN_REGEX", raising=False)

    from be.app.main import create_app

    client = TestClient(create_app())
    resp = client.options(
        "/api/v1/menu/items",
        headers={
            "Origin": "https://sanmai-app-demo.pages.dev",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert "access-control-allow-origin" not in resp.headers
