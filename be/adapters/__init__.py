"""Adapter seam — vendor-agnostic contracts (ABCs) plus zero-credential fakes.

Business logic depends only on the ABCs in each ``base.py`` and the value types in
``types.py``; concrete vendors live behind them. Every interface ships one fake
reference impl (``demo``/``echo``/``generic``/``noop``/``local``/``static``) so the
open-source core boots and the unit tests pass with no external credentials.
"""

from be.adapters.errors import (
    AdapterError,
    ProviderConfigError,
    ProviderPermanent,
    ProviderTransient,
)

__all__ = [
    "AdapterError",
    "ProviderConfigError",
    "ProviderPermanent",
    "ProviderTransient",
]
