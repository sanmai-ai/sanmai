"""Provider factories — map config provider *ids* to concrete adapter instances.

:class:`be.config.Settings` stores which provider each port binds to as a string
(``llm_provider="echo"``, ``storage_provider="local"``, ``identity_provider="static"``).
These pure factories resolve that id to a concrete implementation, raising
:class:`ProviderConfigError` on an unknown id (fail-loud at wiring time, never deep
in a request).

The zero-credential OSS fakes (echo/local/static) always ship in the core. The real
vendor adapters (``gemini``/``gcs``/``firebase``) also live in core but import their
vendor SDK lazily *inside* the selected branch — so importing this module needs no
vendor SDKs and no credentials; a real adapter's creds are resolved (and validated
fail-loud) only when that provider is selected and constructed.
"""

from __future__ import annotations

from be.adapters.errors import ProviderConfigError
from be.adapters.identity.base import IdentityProvider
from be.adapters.identity.static import StaticIdentityProvider
from be.adapters.llm.base import LLMProvider
from be.adapters.llm.echo import EchoLLMProvider
from be.adapters.notify.base import Notifier
from be.adapters.notify.noop import NoopNotifier
from be.adapters.payments.base import PaymentProvider
from be.adapters.payments.demo import DemoPaymentProvider
from be.adapters.payroll.base import PayrollProfile
from be.adapters.payroll.generic import GenericPayrollProfile
from be.adapters.storage.base import Storage
from be.adapters.storage.local import LocalStorage
from be.config import Settings


def build_llm(settings: Settings) -> LLMProvider:
    """Resolve :data:`settings.llm_provider` to a concrete :class:`LLMProvider`."""
    provider_id = settings.llm_provider
    if provider_id == "echo":
        return EchoLLMProvider()
    if provider_id == "gemini":
        if not settings.gemini_api_key:
            raise ProviderConfigError(
                "llm provider 'gemini' requires SANMAI_GEMINI_API_KEY"
            )
        # Vendor import kept inside the branch so importing providers.py stays SDK-free.
        from be.adapters.llm.gemini import GeminiLLMProvider

        return GeminiLLMProvider(
            api_key=settings.gemini_api_key, model=settings.gemini_model
        )
    raise ProviderConfigError(f"unknown llm provider id: {provider_id!r}")


def build_storage(settings: Settings) -> Storage:
    """Resolve :data:`settings.storage_provider` to a concrete :class:`Storage`."""
    provider_id = settings.storage_provider
    if provider_id == "local":
        return LocalStorage()
    if provider_id == "gcs":
        if not settings.gcs_bucket:
            raise ProviderConfigError(
                "storage provider 'gcs' requires SANMAI_GCS_BUCKET"
            )
        from be.adapters.storage.gcs import GcsStorage

        return GcsStorage(
            bucket=settings.gcs_bucket,
            project=settings.gcs_project,
            public_base_url=settings.gcs_public_base_url,
        )
    raise ProviderConfigError(f"unknown storage provider id: {provider_id!r}")


def build_payments(settings: Settings) -> PaymentProvider:
    """Resolve :data:`settings.payments_provider` to a :class:`PaymentProvider`."""
    provider_id = settings.payments_provider
    if provider_id == "demo":
        return DemoPaymentProvider()
    if provider_id == "tranzila":
        if not (settings.tranzila_api_public_key and settings.tranzila_api_secret_key):
            raise ProviderConfigError(
                "payments provider 'tranzila' requires SANMAI_TRANZILA_API_PUBLIC_KEY "
                "and SANMAI_TRANZILA_API_SECRET_KEY"
            )
        if not settings.tranzila_terminal_name:
            raise ProviderConfigError(
                "payments provider 'tranzila' requires SANMAI_TRANZILA_TERMINAL_NAME"
            )
        from be.adapters.payments.tranzila import TranzilaPaymentProvider

        return TranzilaPaymentProvider(
            api_public_key=settings.tranzila_api_public_key,
            api_secret_key=settings.tranzila_api_secret_key,
            terminal_name=settings.tranzila_terminal_name,
            emv_pos_id=settings.tranzila_emv_pos_id,
            paybridge_url=settings.tranzila_paybridge_url,
        )
    raise ProviderConfigError(f"unknown payments provider id: {provider_id!r}")


def build_notify(settings: Settings) -> Notifier:
    """Resolve :data:`settings.notify_provider` to a :class:`Notifier`."""
    provider_id = settings.notify_provider
    if provider_id == "noop":
        return NoopNotifier()
    if provider_id in ("sanmai", "telegram", "gmail", "sms"):
        telegram = gmail = sms = None
        if provider_id in ("sanmai", "telegram"):
            if not settings.telegram_bot_token:
                raise ProviderConfigError(
                    "notify 'telegram' requires SANMAI_TELEGRAM_BOT_TOKEN"
                )
            from be.adapters.notify.telegram import TelegramNotifier

            telegram = TelegramNotifier(
                bot_token=settings.telegram_bot_token,
                default_chat_id=settings.telegram_chat_id,
            )
        if provider_id in ("sanmai", "gmail"):
            if not settings.gmail_oauth_json:
                raise ProviderConfigError(
                    "notify 'gmail' requires SANMAI_GMAIL_OAUTH_JSON"
                )
            from be.adapters.notify.gmail import GmailNotifier

            gmail = GmailNotifier(
                oauth_json=settings.gmail_oauth_json,
                sender=settings.gmail_sender,
            )
        if provider_id in ("sanmai", "sms"):
            if not (settings.sms_gateway_url and settings.sms_api_key):
                raise ProviderConfigError(
                    "notify 'sms' requires SANMAI_SMS_GATEWAY_URL and SANMAI_SMS_API_KEY"
                )
            from be.adapters.notify.sms import SmsNotifier

            sms = SmsNotifier(
                gateway_url=settings.sms_gateway_url,
                api_key=settings.sms_api_key,
                sender_name=settings.sms_sender_name or "SanMai",
            )
        if provider_id == "sanmai":
            from be.adapters.notify.sanmai import SanMaiNotifier

            return SanMaiNotifier(telegram=telegram, gmail=gmail, sms=sms)
        # single-channel deploy: return the one built notifier
        single = telegram or gmail or sms
        assert single is not None  # guaranteed by the branches above
        return single
    raise ProviderConfigError(f"unknown notify provider id: {provider_id!r}")


def build_payroll(settings: Settings) -> PayrollProfile:
    """Resolve :data:`settings.payroll_profile` to a concrete :class:`PayrollProfile`.

    ``"generic"`` is the locale-neutral OSS default (no overtime tiering); ``"il"``
    binds the Israeli labor-law profile. Both are dependency- and credential-free.
    """
    profile_id = settings.payroll_profile
    if profile_id == "generic":
        return GenericPayrollProfile()
    if profile_id == "il":
        # Locale profile kept in its own module so IL constants never load unless selected.
        from be.adapters.payroll.il import IsraeliPayrollProfile

        return IsraeliPayrollProfile()
    raise ProviderConfigError(f"unknown payroll profile id: {profile_id!r}")


def build_identity(settings: Settings) -> IdentityProvider:
    """Resolve :data:`settings.identity_provider` to an :class:`IdentityProvider`."""
    provider_id = settings.identity_provider
    if provider_id == "static":
        return StaticIdentityProvider()
    if provider_id == "firebase":
        from be.adapters.identity.firebase import FirebaseIdentityProvider

        return FirebaseIdentityProvider(
            credentials_json=settings.firebase_credentials_json,
            credentials_path=settings.firebase_credentials_path,
            project_id=settings.firebase_project_id,
        )
    raise ProviderConfigError(f"unknown identity provider id: {provider_id!r}")


__all__ = [
    "build_llm",
    "build_storage",
    "build_payments",
    "build_payroll",
    "build_notify",
    "build_identity",
]
