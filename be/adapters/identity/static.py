"""``StaticIdentityProvider`` — offline fake identity.

Decodes a **fake, unsigned** token: a JSON object (optionally base64url-encoded)
carrying ``uid``, ``claims``, and ``venues``. No signature is checked — this exists
only so the OSS core and tests can construct a :class:`Principal` with zero external
auth infra. NEVER use in production.
"""

from __future__ import annotations

import base64
import binascii
import json

from be.adapters.errors import ProviderPermanent
from be.adapters.identity.base import IdentityProvider
from be.adapters.types import Principal


class StaticIdentityProvider(IdentityProvider):
    """Decodes a fake token dict into a Principal — no cryptographic verification."""

    def verify_token(self, jwt: str) -> Principal:
        if not jwt:
            raise ProviderPermanent("empty token")
        payload = self._decode(jwt)
        uid = payload.get("uid")
        if not uid:
            raise ProviderPermanent("token missing 'uid'")
        return Principal(
            uid=str(uid),
            claims=dict(payload.get("claims", {})),
            venues=list(payload.get("venues", [])),
        )

    @staticmethod
    def _decode(jwt: str) -> dict:
        # Accept raw JSON or base64url-encoded JSON.
        try:
            return json.loads(jwt)
        except (json.JSONDecodeError, TypeError):
            pass
        try:
            padded = jwt + "=" * (-len(jwt) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("utf-8"))
            return json.loads(decoded)
        except (binascii.Error, json.JSONDecodeError, ValueError) as exc:
            raise ProviderPermanent(f"undecodable token: {exc}") from exc


__all__ = ["StaticIdentityProvider"]
