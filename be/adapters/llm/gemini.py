"""``GeminiLLMProvider`` — real LLM adapter over the ``google-generativeai`` SDK.

Self-contained: talks to the vendor SDK directly (no ``app.services.*``). Honours the
revised :class:`LLMProvider` ABC in both modes:

* **structured / JSON** — ``schema`` given ⇒ ``response_mime_type="application/json"`` +
  ``response_schema=schema``; ``Completion.text`` is the JSON string and ``Completion.raw``
  the parsed object.
* **free text** — ``schema`` ``None`` ⇒ no response-schema forced; ``Completion.text`` is
  the raw model text and ``Completion.raw`` is ``None``.

``system_prompt`` becomes the model's ``system_instruction``. ``images`` are passed as
inline blobs with their EXPLICIT :class:`ImagePart.mime_type` (never sniffed). ``model`` /
``temperature`` are per-call overrides.

Importing this module needs no key: the SDK import is module-level (cred-free) but
``genai.configure`` runs at construction, and the api_key is validated in ``__init__``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, cast

import google.generativeai as genai
from google.api_core import exceptions as gexc

from be.adapters.errors import ProviderConfigError, ProviderPermanent, ProviderTransient
from be.adapters.llm.base import LLMProvider
from be.adapters.types import Completion, ImagePart

_DEFAULT_MODEL = "gemini-2.5-flash"
_DEFAULT_TEMPERATURE = 0.4

# Vendor exceptions that mean "try again" (5xx / rate-limit / network blip). Mirrors the
# live client, which retries transport errors + 5xx and fails fast on 4xx.
_TRANSIENT = (
    gexc.ServiceUnavailable,
    gexc.DeadlineExceeded,
    gexc.InternalServerError,
    gexc.TooManyRequests,
    gexc.GatewayTimeout,
)
# Vendor exceptions that mean "do not retry" (bad input / 4xx).
_PERMANENT = (
    gexc.InvalidArgument,
    gexc.FailedPrecondition,
    gexc.PermissionDenied,
    gexc.NotFound,
)


class GeminiLLMProvider(LLMProvider):
    """Real Gemini adapter — vendor SDK translation with the adapter error taxonomy."""

    def __init__(self, *, api_key: str, model: str = _DEFAULT_MODEL) -> None:
        if not api_key:
            raise ProviderConfigError("gemini api_key is required")
        self._api_key = api_key
        self._model = model
        # Resolve the credential at construction (not import): fail-loud at wiring.
        genai.configure(api_key=api_key)

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
        generation_config: dict[str, Any] = {
            "temperature": temperature if temperature is not None else _DEFAULT_TEMPERATURE,
        }
        if schema is not None:
            generation_config["response_mime_type"] = "application/json"
            generation_config["response_schema"] = schema

        # Explicit MIME per part — never sniffed (ImagePart carries the type).
        contents: list[Any] = [prompt]
        for part in images or []:
            contents.append({"mime_type": part.mime_type, "data": part.data})

        try:
            client = genai.GenerativeModel(
                model or self._model,
                system_instruction=system_prompt,
            )
            # The SDK call is synchronous — run it off the event loop. Cast the config
            # to Any: the SDK accepts a plain dict but its stub types a union we don't
            # want to depend on.
            resp = await asyncio.to_thread(
                client.generate_content,
                contents,
                generation_config=cast(Any, generation_config),
            )
        except gexc.Unauthenticated as exc:  # bad/expired key
            raise ProviderConfigError(f"gemini authentication failed: {exc}") from exc
        except _PERMANENT as exc:  # 4xx / bad input — retry won't help
            raise ProviderPermanent(f"gemini rejected the request: {exc}") from exc
        except _TRANSIENT as exc:  # 5xx / rate-limit / timeout — retryable
            raise ProviderTransient(f"gemini transient failure: {exc}") from exc
        except gexc.GoogleAPICallError as exc:  # any other API error -> retryable
            raise ProviderTransient(f"gemini API call error: {exc}") from exc
        except (TimeoutError, ConnectionError, OSError) as exc:  # network blip
            raise ProviderTransient(f"gemini network failure: {exc}") from exc

        text = _extract_text(resp)

        if schema is None:
            return Completion(text=text, raw=None)
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError) as exc:  # unparseable JSON — bad output, permanent
            raise ProviderPermanent(f"gemini returned unparseable JSON: {exc}") from exc
        return Completion(text=text, raw=parsed)


def _extract_text(resp: Any) -> str:
    """Pull the model text off a SDK response, mapping empty/blocked output to permanent."""
    try:
        text = resp.text
    except Exception as exc:  # noqa: BLE001 — SDK raises on blocked/empty candidates
        raise ProviderPermanent(f"gemini produced no usable output: {exc}") from exc
    if not text:
        raise ProviderPermanent("gemini returned an empty response")
    return text


__all__ = ["GeminiLLMProvider"]
