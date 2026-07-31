"""Unit tests for the real GeminiLLMProvider — vendor SDK fully mocked, no network.

Assert request translation (prompt/system_prompt/ImagePart/model/temp, structured vs
free-text) and the error-taxonomy mapping (Transient/Permanent/ConfigError). No real
API key, no network call.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

from be.adapters.errors import ProviderConfigError, ProviderPermanent, ProviderTransient
from be.adapters.llm.base import LLMProvider
from be.adapters.llm.gemini import GeminiLLMProvider
from be.adapters.types import ImagePart


def _provider() -> GeminiLLMProvider:
    with mock.patch("google.generativeai.configure"):
        return GeminiLLMProvider(api_key="test-key", model="gemini-2.5-flash")


def _patch_model(response_text: str):  # type: ignore[no-untyped-def]
    """Patch GenerativeModel so generate_content returns a resp with .text set."""
    resp = mock.Mock()
    resp.text = response_text
    model = mock.Mock()
    model.generate_content.return_value = resp
    return mock.patch("google.generativeai.GenerativeModel", return_value=model), model


def test_import_is_credential_free() -> None:
    import importlib

    import be.adapters.llm.gemini as mod

    importlib.reload(mod)  # importing must not need a key / network


def test_empty_api_key_raises_config_error() -> None:
    with pytest.raises(ProviderConfigError):
        GeminiLLMProvider(api_key="")


def test_is_llm_provider() -> None:
    assert isinstance(_provider(), LLMProvider)


async def test_structured_mode_translation() -> None:
    prov = _provider()
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    patcher, model = _patch_model(json.dumps({"x": "hi"}))
    with patcher as gm_cls:
        out = await prov.complete(
            "user prompt",
            system_prompt="be terse",
            schema=schema,
            images=[ImagePart(data=b"IMG", mime_type="image/png")],
            model="gemini-2.5-pro",
            temperature=0.1,
        )

    # per-call model override + system_instruction threaded into GenerativeModel
    gm_cls.assert_called_once_with("gemini-2.5-pro", system_instruction="be terse")
    args, kwargs = model.generate_content.call_args
    contents = args[0]
    assert contents[0] == "user prompt"
    # ImagePart.mime_type passed verbatim (not sniffed), raw bytes inline
    assert contents[1] == {"mime_type": "image/png", "data": b"IMG"}
    cfg = kwargs["generation_config"]
    assert cfg["response_mime_type"] == "application/json"
    assert cfg["response_schema"] == schema
    assert cfg["temperature"] == 0.1

    assert out.text == json.dumps({"x": "hi"})
    assert out.raw == {"x": "hi"}


async def test_free_text_mode_no_schema() -> None:
    prov = _provider()
    patcher, model = _patch_model("plain answer")
    with patcher:
        out = await prov.complete("hello")

    _, kwargs = model.generate_content.call_args
    cfg = kwargs["generation_config"]
    assert "response_schema" not in cfg
    assert "response_mime_type" not in cfg
    assert cfg["temperature"] == 0.4  # default
    assert out.text == "plain answer"
    assert out.raw is None


async def test_structured_unparseable_json_is_permanent() -> None:
    prov = _provider()
    patcher, _ = _patch_model("not json{")
    with patcher, pytest.raises(ProviderPermanent):
        await prov.complete("p", schema={"type": "object"})


async def test_empty_response_is_permanent() -> None:
    prov = _provider()
    patcher, _ = _patch_model("")
    with patcher, pytest.raises(ProviderPermanent):
        await prov.complete("p")


@pytest.mark.parametrize(
    "exc",
    [
        gexc.ServiceUnavailable("503"),
        gexc.DeadlineExceeded("timeout"),
        gexc.InternalServerError("500"),
        gexc.TooManyRequests("429"),
    ],
)
async def test_transient_error_mapping(exc: Exception) -> None:
    prov = _provider()
    model = mock.Mock()
    model.generate_content.side_effect = exc
    with mock.patch("google.generativeai.GenerativeModel", return_value=model):
        with pytest.raises(ProviderTransient):
            await prov.complete("p")


@pytest.mark.parametrize(
    "exc",
    [
        gexc.InvalidArgument("bad"),
        gexc.FailedPrecondition("nope"),
        gexc.PermissionDenied("denied"),
    ],
)
async def test_permanent_error_mapping(exc: Exception) -> None:
    prov = _provider()
    model = mock.Mock()
    model.generate_content.side_effect = exc
    with mock.patch("google.generativeai.GenerativeModel", return_value=model):
        with pytest.raises(ProviderPermanent):
            await prov.complete("p")


async def test_unauthenticated_maps_to_config_error() -> None:
    prov = _provider()
    model = mock.Mock()
    model.generate_content.side_effect = gexc.Unauthenticated("bad key")
    with mock.patch("google.generativeai.GenerativeModel", return_value=model):
        with pytest.raises(ProviderConfigError):
            await prov.complete("p")
