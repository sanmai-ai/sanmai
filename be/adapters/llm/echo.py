"""``EchoLLMProvider`` — offline fake that echoes the prompt back.

Deterministic, no network, no key. Records every argument it was handed in
``Completion.raw`` so tests can assert they were threaded through. In structured
mode (``schema`` given) ``text`` is valid JSON; in free-text mode it is the prompt.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from be.adapters.llm.base import LLMProvider
from be.adapters.types import Completion, ImagePart


class EchoLLMProvider(LLMProvider):
    """Echoes the prompt — verbatim (free text) or wrapped in JSON (schema mode)."""

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
        raw: dict[str, Any] = {
            "prompt": prompt,
            "system_prompt": system_prompt,
            "schema": schema,
            "image_count": len(images) if images else 0,
            "image_mimes": [p.mime_type for p in images] if images else [],
            "model": model,
            "temperature": temperature,
            "mode": "json" if schema is not None else "text",
        }
        text = json.dumps({"echo": prompt}, ensure_ascii=False) if schema is not None else prompt
        return Completion(text=text, raw=raw)


__all__ = ["EchoLLMProvider"]
