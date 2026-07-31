"""Minimal FastAPI app for the SanMai AI core — health & readiness only.

``/healthz`` is a pure liveness probe (always ``ok`` if the process is up).
``/readyz`` is a readiness probe that verifies configuration actually loads via
:func:`be.config.get_settings` and returns HTTP 503 if it does not, so an
orchestrator won't route traffic to a mis-configured instance.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="SanMai AI Core", version="0.0.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is running and can serve requests."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness: configuration loads cleanly (env-only, fail-loud)."""
    try:
        # Imported lazily so a missing/typo'd config surfaces here as 503
        # rather than crashing the whole process at import time.
        from be.config import get_settings

        get_settings()
    except Exception as exc:  # noqa: BLE001 — any config failure => not ready
        return JSONResponse(
            status_code=503,
            content={"status": "not-ready", "detail": str(exc)},
        )
    return JSONResponse(status_code=200, content={"status": "ready"})
