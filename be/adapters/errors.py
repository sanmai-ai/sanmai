"""Adapter error hierarchy — the frozen contract every provider raises against.

The three subclasses encode the caller's retry decision so business logic never
needs to know which vendor it is talking to:

* ``ProviderTransient``  — retryable (network blip, 5xx, rate-limit).
* ``ProviderPermanent``  — do NOT retry (declined card, auth reject, bad input).
* ``ProviderConfigError`` — misconfiguration; should fail at boot, not at runtime.
"""

from __future__ import annotations


class AdapterError(Exception):
    """Base class for every error raised by an adapter."""


class ProviderTransient(AdapterError):
    """Retryable failure — safe to retry with the same idempotency key."""


class ProviderPermanent(AdapterError):
    """Non-retryable failure — retrying will not help (declined, rejected, invalid)."""


class ProviderConfigError(AdapterError):
    """Missing or invalid configuration/credential. Surface at boot, fail loud."""
