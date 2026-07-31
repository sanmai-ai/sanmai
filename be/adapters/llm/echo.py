"""``EchoLLMProvider`` — offline fake that echoes the prompt back.

Deterministic, no network, no key. Records what it was asked (image count, whether a
schema was supplied) in ``Completion.raw`` so tests can assert the arguments were
threaded through correctly.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from be.adapters.llm.base import LLMProvider
from be.adapters.types import Completion


class EchoLLMProvider(LLMProvider):
    """Returns the prompt verbatim as the completion text."""

    async def complete(
        self,
        prompt: str,
        images: Sequence[bytes] | None = None,
        schema: dict[str, Any] | None = None,
    ) -> Completion:
        raw: dict[str, Any] = {
            "prompt": prompt,
            "image_count": len(images) if images else 0,
            "schema": schema,
        }
        return Completion(text=prompt, raw=raw)


__all__ = ["EchoLLMProvider"]
