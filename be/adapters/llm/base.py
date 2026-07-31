"""``LLMProvider`` — the model-vendor seam.

One method: ``complete``, running in two modes decided by ``schema``:

* **structured / JSON** when ``schema`` is given (a provider-native response schema,
  e.g. a Gemini ``responseSchema``) — ``Completion.text`` is the JSON string and
  ``Completion.raw`` the parsed object;
* **free text** when ``schema`` is ``None`` — ``Completion.text`` is the raw model
  text and ``Completion.raw`` may be ``None``.

``system_prompt`` carries the heavy instruction (every real caller uses it).
``images`` are explicit :class:`ImagePart` (bytes + MIME, never sniffed) so images
and PDFs share the seam. ``model`` / ``temperature`` are optional per-call overrides.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from be.adapters.types import Completion, ImagePart


class LLMProvider(ABC):
    """Vendor-neutral LLM contract."""

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        schema: dict[str, Any] | None = None,
        images: Sequence[ImagePart] | None = None,
        model: str | None = None,
        temperature: float | None = None,
    ) -> Completion:
        """Complete ``prompt``.

        ``schema`` present ⇒ structured JSON mode; ``schema`` ``None`` ⇒ free text.
        Optionally multimodal (``images``) and/or overriding ``model`` /
        ``temperature`` for this one call.
        """


__all__ = ["LLMProvider"]
