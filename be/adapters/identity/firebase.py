"""``FirebaseIdentityProvider`` — real identity adapter over ``firebase-admin``.

Self-contained: verifies ID tokens with the Firebase Admin SDK directly (no
``app.core.auth``, no FastAPI, no DB authz). ``verify_token`` checks the JWT's
signature/expiry/audience against Google's JWKS and maps the decoded claims into a
vendor-neutral :class:`Principal`.

Authentication only — authorization (the ``admin_users`` lookup, page gating) and claim
WRITES (``set_custom_user_claims``) live with callers, out of ABC scope.

Credentials resolve at construction (inline SA JSON, a key-file path, or Application
Default Credentials on Cloud Run). Importing this module needs no creds/SDK: the SDK
import is lazy (inside ``__init__`` / ``verify_token``).
"""

from __future__ import annotations

import json

from be.adapters.errors import ProviderConfigError, ProviderPermanent, ProviderTransient
from be.adapters.identity.base import IdentityProvider
from be.adapters.types import Principal

_APP_NAME = "sanmai-identity"


class FirebaseIdentityProvider(IdentityProvider):
    """Real Firebase adapter — verifies an ID token into a :class:`Principal`."""

    def __init__(
        self,
        *,
        credentials_json: str | None = None,
        credentials_path: str | None = None,
        project_id: str | None = None,
    ) -> None:
        # Lazy SDK import so importing this module needs no firebase-admin / creds.
        import firebase_admin
        from firebase_admin import credentials as fb_credentials

        try:
            if credentials_json:
                cred = fb_credentials.Certificate(json.loads(credentials_json))
            elif credentials_path:
                cred = fb_credentials.Certificate(credentials_path)
            else:
                cred = fb_credentials.ApplicationDefault()  # Cloud Run ADC
            options = {"projectId": project_id} if project_id else None
            # A NAMED app so we never clobber a global default app another adapter owns.
            self._app = firebase_admin.initialize_app(cred, options, name=_APP_NAME)
        except ValueError:
            # initialize_app raises ValueError if the named app already exists — reuse it.
            self._app = firebase_admin.get_app(_APP_NAME)
        except Exception as exc:  # noqa: BLE001 — bad SA JSON / creds -> fail loud at wiring
            raise ProviderConfigError(f"firebase init failed: {exc}") from exc

    def verify_token(self, jwt: str) -> Principal:
        from firebase_admin import auth as fb_auth

        try:
            decoded = fb_auth.verify_id_token(jwt, app=self._app)
        except fb_auth.CertificateFetchError as exc:  # JWKS network blip -> retryable
            raise ProviderTransient(f"JWKS fetch failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — expired/revoked/invalid/disabled -> 401/403
            raise ProviderPermanent(f"token verification failed: {exc}") from exc

        uid = decoded.get("uid") or decoded.get("sub") or ""
        # venues is always [] — SanMai is single-venue; the token carries no venue scope.
        return Principal(uid=str(uid), claims=dict(decoded), venues=[])


__all__ = ["FirebaseIdentityProvider"]
