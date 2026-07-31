"""``LLMProvider`` — the model-vendor seam.

One method: ``complete``. Optional ``images`` (multi-modal prompts) and ``schema``
(a provider-native structured-output schema, e.g. a Gemini ``responseSchema``). The
return is a :class:`Completion` carrying the text plus the raw payload for callers
that need token counts or the parsed JSON.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from be.adapters.types import Completion


class LLMProvider(ABC):
    """Vendor-neutral LLM contract."""

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        images: Sequence[bytes] | None = None,
        schema: dict[str, Any] | None = None,
    ) -> Completion:
        """Complete ``prompt``, optionally multi-modal and/or schema-constrained."""


__all__ = ["LLMProvider"]
