"""FastAPI app factory for the SanMai AI core.

:func:`create_app` builds the app, mounts the health/readiness probes, and
``include_router``s every registered domain router (:data:`DOMAIN_ROUTERS`). The menu
domain is the first real domain and the template for the rest. A module-level
``app = create_app()`` is kept for uvicorn / Cloud Run probes.

``/healthz`` is a pure liveness probe (always ``ok`` if the process is up).
``/readyz`` is a readiness probe that verifies configuration loads via
:func:`be.config.get_settings` and returns HTTP 503 if it does not.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from be.app.domains.analytics import router as analytics_router
from be.app.domains.careers import router as careers_router
from be.app.domains.clock import router as clock_router
from be.app.domains.forms import router as forms_router
from be.app.domains.hr import router as hr_router
from be.app.domains.inventory import router as inventory_router
from be.app.domains.menu import router as menu_router
from be.app.domains.payroll import router as payroll_router
from be.app.domains.scheduling import router as scheduling_router
from be.app.domains.suggestions import router as suggestions_router
from be.app.domains.tasks import router as tasks_router

# Registry of domain routers mounted into every app. Add a domain by appending here.
DOMAIN_ROUTERS: tuple[APIRouter, ...] = (
    menu_router,
    inventory_router,
    hr_router,
    scheduling_router,
    clock_router,
    tasks_router,
    payroll_router,
    forms_router,
    suggestions_router,
    analytics_router,
    careers_router,
)


def _configure_cors(app: FastAPI) -> None:
    """Mount CORS middleware when configured, so browsers can call the API.

    Config-driven and import-safe: any config failure is swallowed here (it will
    surface at ``/readyz``) rather than crashing the module-level ``create_app()``.
    Credentials are OFF (the app authenticates via a Bearer token, not cookies), so
    a wildcard/regex Origin is safe. When neither exact origins nor a regex are set,
    no middleware is mounted at all.
    """
    try:
        from be.config import get_settings

        settings = get_settings()
    except Exception:  # noqa: BLE001 — config errors surface at /readyz, not import
        return

    origins = settings.cors_origins_list
    regex = settings.cors_origin_regex
    if not origins and not regex:
        return

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=regex,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


def create_app() -> FastAPI:
    """Build the FastAPI app: probes + all registered domain routers."""
    app = FastAPI(title="SanMai AI Core", version="0.0.0")

    _configure_cors(app)

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

    for router in DOMAIN_ROUTERS:
        app.include_router(router, prefix="/api/v1")

    return app


app = create_app()
