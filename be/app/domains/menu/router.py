"""Menu admin router — thin endpoints delegating to crud/service via the seams.

Every route is admin-gated through the :class:`IdentityProvider` seam (Bearer token ->
:class:`Principal`, ``admin`` claim required). Vendor work (LLM translate /
parse-ingredients, image storage) goes through the injected ABCs only. Versions and
recipes are company-owned but venue_id-threaded (``VenueDep``) so the store is
venue-ready. The read-copy projection is a no-op seam (Firestore sync deferred).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from be.adapters.llm.base import LLMProvider
from be.adapters.storage.base import Storage
from be.app.deps import AdminDep, DbDep, VenueDep, get_llm, get_storage
from be.app.domains.menu import crud, service
from be.app.domains.menu.projection import MenuProjection, NullMenuProjection
from be.app.domains.menu.schemas import (
    ParseIngredientsResponse,
    RecipeLine,
    RecipeResponse,
    SaveRecipeRequest,
    SaveVersionRequest,
    TranslateTextRequest,
    TranslateTextResponse,
    UploadImageResponse,
    VersionListResponse,
    VersionResponse,
)

router = APIRouter(prefix="/admin/menu", tags=["menu"])

ALLOWED_IMAGE_PREFIX = "image/"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_IMAGE_URL_TTL = 3600


def get_menu_projection() -> MenuProjection:
    """Read-copy projection seam. No-op until a document-store adapter is wired."""
    return NullMenuProjection()


MenuProjectionDep = Annotated[MenuProjection, Depends(get_menu_projection)]
LLMDep = Annotated[LLMProvider, Depends(get_llm)]
StorageDep = Annotated[Storage, Depends(get_storage)]


async def _read_image_or_400(file: UploadFile) -> tuple[bytes, str]:
    content_type = (file.content_type or "").lower()
    if not content_type.startswith(ALLOWED_IMAGE_PREFIX):
        raise HTTPException(400, "File is not an image")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Image too large (max 8MB)")
    return data, content_type


# --- AI translate -----------------------------------------------------------
@router.post("/translate", response_model=TranslateTextResponse)
async def translate_menu_text(
    payload: TranslateTextRequest,
    _admin: AdminDep,
    llm: LLMDep,
) -> TranslateTextResponse:
    """AI-translate a single menu string from ``source`` to ``target``."""
    if not payload.text.strip():
        raise HTTPException(400, "text is required")
    if not payload.source or not payload.target:
        raise HTTPException(400, "source and target languages are required")
    if payload.source == payload.target:
        raise HTTPException(400, "source and target languages must differ")
    translated = await service.translate_text(
        llm,
        text=payload.text,
        source=payload.source,
        target=payload.target,
        field=payload.field,
    )
    if not translated:
        raise HTTPException(502, "Translation failed — please try again.")
    return TranslateTextResponse(text=translated)


# --- Image upload -----------------------------------------------------------
@router.post("/upload-image", response_model=UploadImageResponse)
async def upload_menu_image(
    _admin: AdminDep,
    storage: StorageDep,
    file: UploadFile = File(...),
    item_id: str = Form(""),
    name: str = Form(""),
    slot: str = Form("item"),
) -> UploadImageResponse:
    """Upload a menu/option image via the storage seam; return its key + access URL."""
    data, content_type = await _read_image_or_400(file)
    key = service.build_image_key(
        item_id=item_id,
        name=name,
        slot=slot,
        content_type=content_type,
        filename=file.filename,
    )
    stored_key = await storage.put(key, data, content_type)
    return UploadImageResponse(key=stored_key, url=storage.signed_url(stored_key, _IMAGE_URL_TTL))


# --- Ingredient parsing -----------------------------------------------------
@router.post("/parse-ingredients", response_model=ParseIngredientsResponse)
async def parse_ingredients_image(
    _admin: AdminDep,
    llm: LLMDep,
    file: UploadFile = File(...),
) -> ParseIngredientsResponse:
    """OCR/parse a photo of an ingredient label into a bilingual list."""
    data, content_type = await _read_image_or_400(file)
    result = await service.parse_ingredients(llm, image=data, mime_type=content_type)
    if not result:
        raise HTTPException(502, "Could not read ingredients — try a clearer photo.")
    return ParseIngredientsResponse(en=result.get("en", ""), he=result.get("he", ""))


# --- Versioned full-item history --------------------------------------------
@router.post("/items/{item_id}/versions", response_model=VersionResponse)
async def save_menu_version(
    item_id: str,
    payload: SaveVersionRequest,
    admin: AdminDep,
    venue_id: VenueDep,
    session: DbDep,
    projection: MenuProjectionDep,
) -> VersionResponse:
    """Record a new full-item version and publish it to the read copy (no-op today)."""
    snapshot = crud.clean_snapshot(payload.snapshot)
    name = snapshot.get("name") or {}
    if isinstance(name, dict):
        if not (name.get("en") or name.get("he")):
            raise HTTPException(400, "Item needs a name in at least one language.")
    elif not name:
        raise HTTPException(400, "Item needs a name.")

    baseline = projection.read_snapshot(item_id, venue_id)
    version = await crud.save_version(
        session,
        item_id=item_id,
        venue_id=venue_id,
        snapshot=snapshot,
        changed_by=admin.uid,
        baseline_snapshot=baseline,
    )
    projection.publish(item_id, venue_id, version["snapshot"])
    return VersionResponse(version=version)  # type: ignore[arg-type]


@router.get("/items/{item_id}/versions", response_model=VersionListResponse)
async def list_menu_versions(
    item_id: str,
    _admin: AdminDep,
    venue_id: VenueDep,
    session: DbDep,
) -> VersionListResponse:
    """Full edit history for an item, newest first."""
    versions = await crud.list_versions(session, item_id=item_id, venue_id=venue_id)
    return VersionListResponse(versions=versions)  # type: ignore[arg-type]


@router.post("/items/{item_id}/versions/{version_id}/restore", response_model=VersionResponse)
async def restore_menu_version(
    item_id: str,
    version_id: int,
    admin: AdminDep,
    venue_id: VenueDep,
    session: DbDep,
    projection: MenuProjectionDep,
) -> VersionResponse:
    """Restore a previous version (append-only) and republish to the read copy."""
    version = await crud.restore_version(
        session,
        item_id=item_id,
        venue_id=venue_id,
        version_id=version_id,
        changed_by=admin.uid,
    )
    if version is None:
        raise HTTPException(404, "Version not found for this item.")
    projection.publish(item_id, venue_id, version["snapshot"])
    return VersionResponse(version=version)  # type: ignore[arg-type]


# --- Recipes ----------------------------------------------------------------
@router.get("/items/{item_id}/recipe", response_model=RecipeResponse)
async def get_menu_recipe(
    item_id: str,
    _admin: AdminDep,
    venue_id: VenueDep,
    session: DbDep,
) -> RecipeResponse:
    """All recipe lines for a menu item."""
    lines = await crud.get_recipe_for_item(session, menu_item_id=item_id, venue_id=venue_id)
    return RecipeResponse(lines=[RecipeLine(**line) for line in lines])


@router.put("/items/{item_id}/recipe", response_model=RecipeResponse)
async def put_menu_recipe(
    item_id: str,
    payload: SaveRecipeRequest,
    admin: AdminDep,
    venue_id: VenueDep,
    session: DbDep,
) -> RecipeResponse:
    """Replace the full recipe for a menu item (delete-then-insert)."""
    incoming = [line.model_dump() for line in payload.lines]
    try:
        saved = await crud.replace_recipe_for_item(
            session,
            menu_item_id=item_id,
            venue_id=venue_id,
            updated_by=admin.uid,
            lines=incoming,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RecipeResponse(lines=[RecipeLine(**line) for line in saved])


__all__ = ["router", "get_menu_projection"]
