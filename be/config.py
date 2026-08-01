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
    payroll_profile: str = Field(
        default="generic",
        description="Payroll pay-computation profile id, e.g. 'generic' | 'il'.",
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

    # --- CORS (browser cross-origin access) ---
    cors_origins: str | None = Field(
        default=None,
        description=(
            "Comma-separated list of exact browser Origins allowed to call the API "
            "(SANMAI_CORS_ORIGINS), e.g. 'https://app.example.com,http://localhost:8000'. "
            "When unset (and no regex is set) no CORS middleware is mounted."
        ),
    )
    cors_origin_regex: str | None = Field(
        default=None,
        description=(
            "Optional regex matching allowed browser Origins (SANMAI_CORS_ORIGIN_REGEX), "
            r"e.g. 'https://.*\.pages\.dev' for Cloudflare Pages preview deploys."
        ),
    )

    # --- analytics (sales pipeline) ---
    analytics_ingest_token: str | None = Field(
        default=None,
        description=(
            "Shared secret for the server-to-server sales-upload path "
            "(SANMAI_ANALYTICS_INGEST_TOKEN). When set, a matching X-Ingest-Token "
            "header authorizes an automated upload without an admin Bearer token; "
            "when unset the header path is disabled (admin auth only)."
        ),
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

    # --- payments: tranzila (required only when payments_provider='tranzila') ---
    tranzila_api_public_key: str | None = Field(
        default=None,
        description="Tranzila REST public app-key (SANMAI_TRANZILA_API_PUBLIC_KEY).",
    )
    tranzila_api_secret_key: str | None = Field(
        default=None,
        description="Tranzila REST private/secret key for HMAC (SANMAI_TRANZILA_API_SECRET_KEY).",
    )
    tranzila_terminal_name: str | None = Field(
        default=None,
        description="Tranzila terminal/supplier name (SANMAI_TRANZILA_TERMINAL_NAME).",
    )
    tranzila_emv_pos_id: str | None = Field(
        default=None,
        description="EMV Pay Bridge pos_id for the paired pinpad (SANMAI_TRANZILA_EMV_POS_ID).",
    )
    tranzila_paybridge_url: str = Field(
        default="https://pinpad.tranzila.com/pinpad/PinpadServiceHandler.ashx",
        description="EMV Pay Bridge CGI URL (SANMAI_TRANZILA_PAYBRIDGE_URL).",
    )

    # --- notify: telegram / gmail / sms (required per enabled channel) ---
    telegram_bot_token: str | None = Field(
        default=None,
        description="Telegram Bot API token (SANMAI_TELEGRAM_BOT_TOKEN).",
    )
    telegram_chat_id: int | None = Field(
        default=None,
        description="Default Telegram chat id for approvals (SANMAI_TELEGRAM_CHAT_ID).",
    )
    gmail_oauth_json: str | None = Field(
        default=None,
        description="Gmail OAuth blob (client_id/secret/refresh_token) (SANMAI_GMAIL_OAUTH_JSON).",
    )
    gmail_sender: str | None = Field(
        default=None,
        description="Optional verified 'send as' From alias for Gmail (SANMAI_GMAIL_SENDER).",
    )
    sms_gateway_url: str | None = Field(
        default=None,
        description="SMS gateway endpoint URL (SANMAI_SMS_GATEWAY_URL).",
    )
    sms_api_key: str | None = Field(
        default=None,
        description="SMS gateway API key (SANMAI_SMS_API_KEY).",
    )
    sms_sender_name: str | None = Field(
        default=None,
        description="SMS sender/from name (SANMAI_SMS_SENDER_NAME).",
    )


    @property
    def cors_origins_list(self) -> list[str]:
        """Parsed, whitespace-trimmed list of exact allowed Origins (empty if unset)."""
        if not self.cors_origins:
            return []
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide cached ``Settings`` instance.

    Raises ``pydantic.ValidationError`` on the first call if any required key is
    missing from the environment. Cached so config is parsed once per process.
    """
    return Settings()  # type: ignore[call-arg]  # values come from the environment
