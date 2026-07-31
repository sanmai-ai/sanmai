"""Single source of truth for runtime configuration.

``Settings`` is env-only and **fail-loud**: there are NO defaults for required
keys, so a missing or typo'd environment variable raises at construction time
(boot) rather than blowing up deep inside a request handler.

Every key is read with the ``SANMAI_`` prefix, e.g. the field ``database_url``
is populated from ``SANMAI_DATABASE_URL``.

``config_schema_version`` is the F2 compatibility contract: the deploy pipeline
refuses to ship a core whose schema version is older than an overlay requires.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, sourced exclusively from the environment.

    Required fields have no default. Constructing ``Settings()`` without them in
    the environment raises ``pydantic.ValidationError`` — this is intentional:
    misconfiguration must surface at boot, never silently at runtime.
    """

    model_config = SettingsConfigDict(
        env_prefix="SANMAI_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    # --- compatibility contract (F2) ---
    config_schema_version: int = Field(
        ...,
        description="Schema version of this config; deploy gate refuses too-low cores.",
    )

    # --- required infrastructure ---
    database_url: str = Field(
        ...,
        description="SQLAlchemy DB URL (sqlite for tests, postgres in prod).",
    )
    env: str = Field(
        ...,
        description="Deployment environment name, e.g. 'dev' | 'test' | 'prod'.",
    )
    default_venue_id: str = Field(
        default="default",
        description=(
            "Fallback venue id for company-scoped domains that are venue_id-ready "
            "but not yet multi-venue. Used when a Principal carries no venue scope."
        ),
    )

    # --- adapter selection (which provider each port binds to) ---
    payments_provider: str = Field(
        ...,
        description="Payment adapter id, e.g. 'demo' | 'tranzila'.",
    )
    llm_provider: str = Field(
        ...,
        description="LLM adapter id, e.g. 'echo' | 'gemini'.",
    )
    fiscal_profile: str = Field(
        ...,
        description="Fiscal profile id, e.g. 'generic' | 'il'.",
    )
    notify_provider: str = Field(
        default="noop",
        description="Notifier adapter id, e.g. 'noop' | 'telegram'.",
    )
    storage_provider: str = Field(
        default="local",
        description="Storage adapter id, e.g. 'local' | 'gcs'.",
    )
    identity_provider: str = Field(
        default="static",
        description="Identity adapter id, e.g. 'static' | 'firebase'.",
    )

    # --- vendor credentials (OPTIONAL — required only when the matching real provider
    #     is selected; resolved at provider construction, never at import) ---
    gemini_api_key: str | None = Field(
        default=None,
        description="API key for the 'gemini' LLM provider (SANMAI_GEMINI_API_KEY).",
    )
    gemini_model: str = Field(
        default="gemini-2.5-flash",
        description="Model id for the 'gemini' LLM provider (SANMAI_GEMINI_MODEL).",
    )
    gcs_bucket: str | None = Field(
        default=None,
        description="Bucket name for the 'gcs' storage provider (SANMAI_GCS_BUCKET).",
    )
    gcs_project: str | None = Field(
        default=None,
        description="GCP project for the 'gcs' storage client (SANMAI_GCS_PROJECT).",
    )
    gcs_public_base_url: str | None = Field(
        default=None,
        description="Optional public base URL for 'gcs' objects (SANMAI_GCS_PUBLIC_BASE_URL).",
    )
    firebase_credentials_json: str | None = Field(
        default=None,
        description="Inline SA JSON for 'firebase' identity (SANMAI_FIREBASE_CREDENTIALS_JSON).",
    )
    firebase_credentials_path: str | None = Field(
        default=None,
        description="SA key-file path for 'firebase' identity (SANMAI_FIREBASE_CREDENTIALS_PATH).",
    )
    firebase_project_id: str | None = Field(
        default=None,
        description="GCP project id for 'firebase' identity (SANMAI_FIREBASE_PROJECT_ID).",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide cached ``Settings`` instance.

    Raises ``pydantic.ValidationError`` on the first call if any required key is
    missing from the environment. Cached so config is parsed once per process.
    """
    return Settings()  # type: ignore[call-arg]  # values come from the environment
