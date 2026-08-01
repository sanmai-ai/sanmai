"""Analytics (sales pipeline) router — thin endpoints, all admin-gated via DB-RBAC.

Every route requires ``require_admin("analytics")`` (verified Bearer -> Principal ->
``admin_users`` lookup), which FIXES the live email-spoof holes across the whole
analytics suite (the previous bespoke check trusted an ``email``/``phone`` query param).
The one exception is the automated upload path: ``POST /analytics/upload`` also accepts a
constant-time-compared ``X-Ingest-Token`` shared secret (server-to-server poller), and
falls back to the admin gate when the token is absent/invalid.

Vendor touch points go through adapters only: the text-to-SQL chat + menu-sync +
suggestions via the injected :class:`LLMProvider` (generated SQL is validated read-only
BEFORE execution — the LLM never writes), and the archived-upload storage/download via
the :class:`Storage` seam (proxy-streamed back — Cloud Run can't sign URLs).

DEFERRED (noted): the BigQuery secondary-sink mirror and the Firestore menu mirror are
seamed away — ``analytics_menu_items`` is populated via the admin sync endpoint only.
"""

from __future__ import annotations

import hmac
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response

from be.adapters.errors import AdapterError, ProviderPermanent
from be.adapters.identity.base import IdentityProvider
from be.adapters.llm.base import LLMProvider
from be.adapters.storage.base import Storage
from be.adapters.types import Principal
from be.app.deps import (
    AdminContext,
    DbDep,
    SettingsDep,
    VenueDep,
    authorize_admin,
    get_identity,
    get_llm,
    get_storage,
    require_admin,
)
from be.app.domains.analytics import crud, ingest, service
from be.app.domains.analytics.schemas import (
    ChatRequest,
    MenuItemsSyncRequest,
    MenuSyncRequest,
    ReprocessRequest,
    SuggestionResolveRequest,
)

router = APIRouter(tags=["analytics"])

AnalyticsAdmin = Annotated[AdminContext, Depends(require_admin("analytics"))]
LLMDep = Annotated[LLMProvider, Depends(get_llm)]
StorageDep = Annotated[Storage, Depends(get_storage)]

GRANULARITIES = {"day", "week", "month"}
MAX_UPLOAD_BYTES = 25_000_000


def _parse_categories(categories: str | None) -> list[str] | None:
    if not categories:
        return None
    vals = [c.strip() for c in categories.split(",") if c.strip()]
    return vals or None


def _parse_range(from_: str | None, to: str | None) -> tuple[str, str]:
    today = date.today()
    try:
        to_date = date.fromisoformat(to) if to else today
        from_date = date.fromisoformat(from_) if from_ else (to_date - timedelta(days=29))
    except ValueError as exc:
        raise HTTPException(400, "Invalid date; expected YYYY-MM-DD") from exc
    if from_date > to_date:
        raise HTTPException(400, "'from' must be on or before 'to'")
    return from_date.isoformat(), to_date.isoformat()


# ── Read endpoints ──────────────────────────────────────────────────


@router.get("/analytics/categories")
async def categories_endpoint(
    _admin: AnalyticsAdmin, venue_id: VenueDep, session: DbDep
) -> list[dict]:
    return await crud.get_categories(session, venue_id=venue_id)


@router.get("/analytics/summary")
async def summary_endpoint(
    _admin: AnalyticsAdmin,
    venue_id: VenueDep,
    session: DbDep,
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    categories: str | None = Query(None),
) -> dict:
    f, t = _parse_range(from_, to)
    return await crud.get_summary(
        session, venue_id=venue_id, from_date=f, to_date=t,
        categories=_parse_categories(categories),
    )


@router.get("/analytics/timeseries")
async def timeseries_endpoint(
    _admin: AnalyticsAdmin,
    venue_id: VenueDep,
    session: DbDep,
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    granularity: str = Query("day"),
    categories: str | None = Query(None),
) -> list[dict]:
    if granularity not in GRANULARITIES:
        raise HTTPException(400, f"granularity must be one of {sorted(GRANULARITIES)}")
    f, t = _parse_range(from_, to)
    return await crud.get_timeseries(
        session, venue_id=venue_id, from_date=f, to_date=t, granularity=granularity,
        categories=_parse_categories(categories),
    )


@router.get("/analytics/category-mix")
async def category_mix_endpoint(
    _admin: AnalyticsAdmin,
    venue_id: VenueDep,
    session: DbDep,
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    categories: str | None = Query(None),
) -> list[dict]:
    f, t = _parse_range(from_, to)
    return await crud.get_category_mix(
        session, venue_id=venue_id, from_date=f, to_date=t,
        categories=_parse_categories(categories),
    )


@router.get("/analytics/top-dishes")
async def top_dishes_endpoint(
    _admin: AnalyticsAdmin,
    venue_id: VenueDep,
    session: DbDep,
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    limit: int = Query(15, ge=1, le=100),
    categories: str | None = Query(None),
) -> list[dict]:
    f, t = _parse_range(from_, to)
    return await crud.get_top_dishes(
        session, venue_id=venue_id, from_date=f, to_date=t, limit=limit,
        categories=_parse_categories(categories),
    )


@router.get("/analytics/component-mix")
async def component_mix_endpoint(
    _admin: AnalyticsAdmin,
    venue_id: VenueDep,
    session: DbDep,
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    categories: str | None = Query(None),
) -> list[dict]:
    """Generic JSON-array-explode report over the per-line ``components`` breakdown."""
    f, t = _parse_range(from_, to)
    return await crud.get_component_mix(
        session, venue_id=venue_id, from_date=f, to_date=t,
        categories=_parse_categories(categories),
    )


@router.get("/analytics/heatmap")
async def heatmap_endpoint(
    _admin: AnalyticsAdmin,
    venue_id: VenueDep,
    session: DbDep,
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    categories: str | None = Query(None),
) -> list[dict]:
    f, t = _parse_range(from_, to)
    return await crud.get_heatmap(
        session, venue_id=venue_id, from_date=f, to_date=t,
        categories=_parse_categories(categories),
    )


@router.get("/analytics/ingestions")
async def ingestions_endpoint(
    _admin: AnalyticsAdmin,
    venue_id: VenueDep,
    session: DbDep,
    limit: int = Query(20, ge=1, le=100),
) -> list[dict]:
    return await crud.get_ingestions(session, venue_id=venue_id, limit=limit)


@router.get("/analytics/processing-runs")
async def processing_runs_endpoint(
    _admin: AnalyticsAdmin,
    venue_id: VenueDep,
    session: DbDep,
    limit: int = Query(20, ge=1, le=200),
) -> list[dict]:
    return await crud.list_processing_runs(session, venue_id=venue_id, limit=limit)


@router.get("/analytics/ingestions/{ing_id}/file")
async def ingestion_file_endpoint(
    ing_id: int, _admin: AnalyticsAdmin, venue_id: VenueDep, session: DbDep,
    storage: StorageDep,
) -> Response:
    """Proxy-stream the archived upload for an ingestion (auth-gated; no signed URL)."""
    row = await crud.get_ingestion_file(session, venue_id=venue_id, ing_id=ing_id)
    if not row or not row["file_path"]:
        raise HTTPException(404, "No stored file for this ingestion")
    try:
        data = await storage.get(row["file_path"])
    except (AdapterError, FileNotFoundError, KeyError) as exc:
        raise HTTPException(404, "file not found") from exc
    name = row["filename"] or "upload"
    media = "text/csv" if name.lower().endswith(".csv") else "application/octet-stream"
    return Response(
        content=data, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ── Upload (admin OR X-Ingest-Token shared secret) ──────────────────


@router.post("/analytics/upload")
async def upload_endpoint(
    settings: SettingsDep,
    session: DbDep,
    storage: StorageDep,
    identity: Annotated[IdentityProvider, Depends(get_identity)],
    file: UploadFile = File(...),
    x_ingest_token: str | None = Header(None, alias="X-Ingest-Token"),
    authorization: str | None = Header(None),
) -> dict:
    """Ingest a CSV / ``.xlsx`` sales export.

    Authorized by EITHER a valid ``X-Ingest-Token`` (server-to-server; source='email')
    OR ``require_admin('analytics')`` (source='manual'). The token path is disabled
    unless ``SANMAI_ANALYTICS_INGEST_TOKEN`` is configured.
    """
    token_ok = bool(
        settings.analytics_ingest_token
        and x_ingest_token
        and hmac.compare_digest(x_ingest_token, settings.analytics_ingest_token)
    )
    venue_id = settings.default_venue_id
    if token_ok:
        source = "email"
    else:
        # Fall back to the admin gate (verified token -> DB-RBAC), replicating the
        # normal require_admin path outside the dependency chain.
        principal = _verify_bearer(identity, authorization)
        venue_id = principal.venues[0] if principal.venues else settings.default_venue_id
        await authorize_admin(session, principal, venue_id, "analytics")
        source = "manual"

    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large")

    return await ingest.ingest_bytes(
        session, storage, venue_id=venue_id, data=data,
        filename=file.filename, source=source,
    )


def _verify_bearer(identity: IdentityProvider, authorization: str | None) -> Principal:
    if not authorization:
        raise HTTPException(
            401, "missing Authorization header", headers={"WWW-Authenticate": "Bearer"}
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            401, "Authorization header must be 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return identity.verify_token(token.strip())
    except ProviderPermanent as exc:
        raise HTTPException(
            401, f"invalid token: {exc}", headers={"WWW-Authenticate": "Bearer"}
        ) from exc


# ── Reprocess ───────────────────────────────────────────────────────


@router.post("/analytics/reprocess")
async def reprocess_endpoint(
    payload: ReprocessRequest, admin: AnalyticsAdmin, venue_id: VenueDep, session: DbDep
) -> dict:
    """Rebuild processed sales from raw — for one upload (``ingestion_id``) or a range."""
    actor = admin.email or admin.uid
    if payload.ingestion_id is not None:
        return await ingest.reprocess(
            session, venue_id=venue_id, ingestion_id=payload.ingestion_id,
            date_from=None, date_to=None, actor=actor,
        )
    try:
        date_from = date.fromisoformat(str(payload.date_from))
        date_to = date.fromisoformat(str(payload.date_to))
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            400, "Provide ingestion_id, or date_from and date_to (YYYY-MM-DD)"
        ) from exc
    if date_from > date_to:
        raise HTTPException(400, "date_from must be on or before date_to")
    return await ingest.reprocess(
        session, venue_id=venue_id, ingestion_id=None,
        date_from=date_from.isoformat(), date_to=date_to.isoformat(), actor=actor,
    )


# ── Text-to-SQL chat ────────────────────────────────────────────────


@router.post("/analytics/chat")
async def chat_endpoint(
    payload: ChatRequest, admin: AnalyticsAdmin, venue_id: VenueDep, session: DbDep,
    llm: LLMDep,
) -> dict:
    """Guarded natural-language Q&A. Generated SQL is validated read-only before it runs."""
    return await service.answer_question(
        llm, session, venue_id=venue_id, question=payload.question,
        history=payload.history, actor=admin.email or admin.uid,
    )


# ── Menu sync / suggestions ─────────────────────────────────────────


@router.post("/analytics/menu/sync")
async def menu_sync_endpoint(
    payload: MenuSyncRequest, _admin: AnalyticsAdmin, venue_id: VenueDep, session: DbDep,
    llm: LLMDep,
) -> dict:
    try:
        return await service.sync_menu(
            llm, session, venue_id=venue_id, dry_run=payload.dry_run
        )
    except AdapterError as exc:
        raise HTTPException(502, f"menu sync failed: {exc}") from exc


@router.post("/analytics/menu-items/sync")
async def menu_items_sync_endpoint(
    payload: MenuItemsSyncRequest, _admin: AnalyticsAdmin, venue_id: VenueDep,
    session: DbDep,
) -> dict:
    items = [it.model_dump() for it in payload.items]
    n = await crud.upsert_menu_items(session, venue_id=venue_id, items=items)
    await session.commit()
    return {"upserted": n}


@router.post("/analytics/menu-suggestions/refresh")
async def menu_suggestions_refresh_endpoint(
    _admin: AnalyticsAdmin, venue_id: VenueDep, session: DbDep, llm: LLMDep
) -> dict:
    try:
        return await service.refresh_suggestions(llm, session, venue_id=venue_id)
    except AdapterError as exc:
        raise HTTPException(502, f"refresh failed: {exc}") from exc


@router.get("/analytics/menu-suggestions")
async def menu_suggestions_list_endpoint(
    _admin: AnalyticsAdmin, venue_id: VenueDep, session: DbDep,
    status: str = Query("open"),
) -> list[dict]:
    return await crud.list_suggestions(session, venue_id=venue_id, status=status)


@router.post("/analytics/menu-suggestions/{sugg_id}/resolve")
async def menu_suggestions_resolve_endpoint(
    sugg_id: int, payload: SuggestionResolveRequest, admin: AnalyticsAdmin,
    venue_id: VenueDep, session: DbDep,
) -> dict:
    if payload.status not in ("approved", "dismissed"):
        raise HTTPException(400, "status must be 'approved' or 'dismissed'")
    sugg = await crud.get_suggestion(session, venue_id=venue_id, sugg_id=sugg_id)
    if not sugg:
        raise HTTPException(404, "Suggestion not found")

    updated = 0
    if payload.status == "approved":
        if sugg["price"] is None:
            raise HTTPException(400, "Cannot map a dish with no price to the catalog")
        name = payload.name or sugg["proposed_name"] or sugg["dish_name"]
        components = (
            payload.components if payload.components is not None
            else sugg["proposed_components"]
        )
        await crud.upsert_menu(
            session, venue_id=venue_id, pos_name=sugg["dish_name"],
            price=float(sugg["price"]), clean_name=name,
            components=components if isinstance(components, list) else [],
        )
        updated = await crud.rematerialize_components(session, venue_id=venue_id)

    await crud.set_suggestion_status(
        session, sugg_id=sugg_id, status=payload.status, actor=admin.email or admin.uid
    )
    await session.commit()
    return {"id": sugg_id, "status": payload.status, "sales_rows_updated": updated}


__all__ = ["router"]
