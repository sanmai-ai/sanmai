"""Unit tests for the real FirebaseIdentityProvider — firebase-admin mocked, no network.

Assert token->Principal translation (no "Bearer " strip, uid/sub fallback, venues=[])
and the error-taxonomy mapping (Transient/Permanent). No real creds, no JWKS fetch.
"""

from __future__ import annotations

from unittest import mock

import pytest
from firebase_admin import auth as fb_auth

from be.adapters.errors import ProviderPermanent, ProviderTransient
from be.adapters.identity.base import IdentityProvider
from be.adapters.identity.firebase import FirebaseIdentityProvider
from be.adapters.types import Principal


def _provider() -> FirebaseIdentityProvider:
    with (
        mock.patch("firebase_admin.credentials.ApplicationDefault"),
        mock.patch("firebase_admin.initialize_app", return_value=mock.Mock()),
    ):
        return FirebaseIdentityProvider()


def test_import_is_credential_free() -> None:
    import importlib

    import be.adapters.identity.firebase as mod

    importlib.reload(mod)  # importing must not need firebase-admin creds


def test_is_identity_provider() -> None:
    assert isinstance(_provider(), IdentityProvider)


def test_construction_uses_certificate_for_inline_json() -> None:
    with (
        mock.patch("firebase_admin.credentials.Certificate") as cert,
        mock.patch("firebase_admin.initialize_app", return_value=mock.Mock()),
    ):
        FirebaseIdentityProvider(credentials_json='{"type": "service_account"}')
        cert.assert_called_once_with({"type": "service_account"})


def test_verify_token_translation() -> None:
    prov = _provider()
    decoded = {"uid": "u42", "email": "a@b.co", "admin": True}
    with mock.patch(
        "firebase_admin.auth.verify_id_token", return_value=decoded
    ) as verify:
        principal = prov.verify_token("raw.jwt.token")

    # raw jwt passed verbatim (no "Bearer " stripping)
    args, kwargs = verify.call_args
    assert args[0] == "raw.jwt.token"
    assert isinstance(principal, Principal)
    assert principal.uid == "u42"
    assert principal.claims == decoded
    assert principal.venues == []  # single-venue: always empty


def test_uid_falls_back_to_sub() -> None:
    prov = _provider()
    with mock.patch(
        "firebase_admin.auth.verify_id_token", return_value={"sub": "sub-99"}
    ):
        assert prov.verify_token("t").uid == "sub-99"


def test_certificate_fetch_error_is_transient() -> None:
    prov = _provider()
    with mock.patch(
        "firebase_admin.auth.verify_id_token",
        side_effect=fb_auth.CertificateFetchError("jwks down", None),
    ):
        with pytest.raises(ProviderTransient):
            prov.verify_token("t")


def test_generic_verify_failure_is_permanent() -> None:
    prov = _provider()
    with mock.patch(
        "firebase_admin.auth.verify_id_token",
        side_effect=ValueError("expired token"),
    ):
        with pytest.raises(ProviderPermanent):
            prov.verify_token("t")
