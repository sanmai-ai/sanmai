"""``IdentityProvider`` — the token-verification seam.

``verify_token`` turns a signed JWT into a trusted :class:`Principal` (stable uid,
verified claims, and venue scoping). Real impls verify signature + expiry against the
issuer's JWKS; a failure raises ``ProviderPermanent`` (bad token) so callers return
401/403 rather than trusting unverified input.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from be.adapters.types import Principal


class IdentityProvider(ABC):
    """Vendor-neutral identity contract."""

    @abstractmethod
    def verify_token(self, jwt: str) -> Principal:
        """Verify ``jwt`` and return the authenticated :class:`Principal`."""


__all__ = ["IdentityProvider"]
