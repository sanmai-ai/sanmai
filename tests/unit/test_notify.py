"""Unit tests for the real notify adapters — vendor HTTP/SDK MOCKED, no network/creds.

Covers: Telegram sendMessage (httpx via respx) success + failure + best-effort no-raise;
SMS gateway POST (respx) success/failure; Gmail via an injected fake service (no SDK/creds);
and SanMaiNotifier per-kind routing incl. unknown kind + unconfigured channel.
"""

from __future__ import annotations

import json
from unittest import mock

import httpx
import respx

from be.adapters.notify.base import Notifier
from be.adapters.notify.gmail import GmailNotifier
from be.adapters.notify.sanmai import SanMaiNotifier
from be.adapters.notify.sms import SmsNotifier
from be.adapters.notify.telegram import TelegramNotifier

_TG_URL = "https://api.telegram.org/bottok/sendMessage"
_SMS_URL = "https://sms.example.com/send"


# --------------------------------------------------------------------------- import-safety


def test_import_is_credential_free() -> None:
    import importlib

    import be.adapters.notify.gmail as g
    import be.adapters.notify.sanmai as s
    import be.adapters.notify.sms as sm
    import be.adapters.notify.telegram as t

    for mod in (g, s, sm, t):
        importlib.reload(mod)


def test_all_conform_to_notifier() -> None:
    assert isinstance(TelegramNotifier(bot_token="tok"), Notifier)
    assert isinstance(SmsNotifier(gateway_url="u", api_key="k"), Notifier)
    assert isinstance(GmailNotifier(oauth_json="{}"), Notifier)
    assert isinstance(SanMaiNotifier(), Notifier)


# ------------------------------------------------------------------------------- telegram


@respx.mock
async def test_telegram_success_returns_message_id() -> None:
    route = respx.post(_TG_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
    )
    n = TelegramNotifier(bot_token="tok", default_chat_id=-100)
    res = await n.send("telegram_approval", {"text": "<b>hi</b>", "reply_markup": {"x": 1}})
    assert res.ok is True and res.detail == "42"
    body = json.loads(route.calls.last.request.content)
    assert body["chat_id"] == -100  # default chat id used
    assert body["parse_mode"] == "HTML"
    assert "reply_markup" in body


@respx.mock
async def test_telegram_explicit_chat_id_overrides_default() -> None:
    route = respx.post(_TG_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )
    n = TelegramNotifier(bot_token="tok", default_chat_id=-100)
    await n.send("telegram_message", {"text": "hi", "chat_id": 777})
    assert json.loads(route.calls.last.request.content)["chat_id"] == 777


@respx.mock
async def test_telegram_api_not_ok_is_failure() -> None:
    respx.post(_TG_URL).mock(return_value=httpx.Response(200, json={"ok": False, "description": "bad"}))
    res = await TelegramNotifier(bot_token="tok", default_chat_id=1).send("telegram_x", {"text": "y"})
    assert res.ok is False


@respx.mock
async def test_telegram_network_error_never_raises() -> None:
    respx.post(_TG_URL).mock(side_effect=httpx.ConnectError("down"))
    res = await TelegramNotifier(bot_token="tok", default_chat_id=1).send("telegram_x", {"text": "y"})
    assert res.ok is False


async def test_telegram_missing_chat_id_is_failure() -> None:
    res = await TelegramNotifier(bot_token="tok").send("telegram_x", {"text": "y"})
    assert res.ok is False and "chat_id" in res.detail


async def test_telegram_missing_token_is_failure() -> None:
    res = await TelegramNotifier(bot_token="").send("telegram_x", {"text": "y", "chat_id": 1})
    assert res.ok is False


# ------------------------------------------------------------------------------------ sms


@respx.mock
async def test_sms_success() -> None:
    route = respx.post(_SMS_URL).mock(return_value=httpx.Response(200, json={"ok": 1}))
    n = SmsNotifier(gateway_url=_SMS_URL, api_key="key", sender_name="Shop")
    res = await n.send("sms_status", {"phone": "+972500000000", "message": "ready"})
    assert res.ok is True
    body = json.loads(route.calls.last.request.content)
    assert body["to"] == "+972500000000"
    assert body["from"] == "Shop"
    assert body["api_key"] == "key"


@respx.mock
async def test_sms_http_error_never_raises() -> None:
    respx.post(_SMS_URL).mock(return_value=httpx.Response(500))
    res = await SmsNotifier(gateway_url=_SMS_URL, api_key="k").send(
        "sms_status", {"phone": "1", "message": "m"}
    )
    assert res.ok is False


async def test_sms_missing_fields_is_failure() -> None:
    res = await SmsNotifier(gateway_url=_SMS_URL, api_key="k").send("sms_status", {"phone": "1"})
    assert res.ok is False


# ---------------------------------------------------------------------------------- gmail


def _fake_service(record: dict) -> mock.Mock:
    service = mock.Mock()
    send = service.users.return_value.messages.return_value.send

    def _capture(*, userId: str, body: dict) -> mock.Mock:  # noqa: N803 — SDK kwarg name
        record["userId"] = userId
        record["body"] = body
        return mock.Mock(execute=mock.Mock(return_value={"id": "msg-1"}))

    send.side_effect = _capture
    return service


async def test_gmail_sends_via_injected_service() -> None:
    record: dict = {}
    n = GmailNotifier(
        oauth_json="{}", sender="from@x.com", service_factory=lambda: _fake_service(record)
    )
    res = await n.send(
        "gmail_accountant",
        {"to": ["acc@x.com"], "subject": "S", "html_body": "<p>hi</p>"},
    )
    assert res.ok is True and res.detail == "msg-1"
    assert record["userId"] == "me"
    assert "raw" in record["body"]  # base64url MIME


async def test_gmail_no_recipients_is_failure() -> None:
    n = GmailNotifier(oauth_json="{}", service_factory=lambda: mock.Mock())
    res = await n.send("gmail_accountant", {"to": [], "subject": "S", "html_body": "x"})
    assert res.ok is False


async def test_gmail_send_error_never_raises() -> None:
    def _factory() -> mock.Mock:
        raise RuntimeError("auth failed")

    n = GmailNotifier(oauth_json="{}", service_factory=_factory)
    res = await n.send("gmail_accountant", {"to": ["a@x.com"], "subject": "S", "html_body": "x"})
    assert res.ok is False and "auth failed" in res.detail


# --------------------------------------------------------------------------- sanmai router


class _Spy(Notifier):
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, dict]] = []

    async def send(self, kind: str, payload: dict):  # type: ignore[no-untyped-def]
        from be.adapters.types import SendResult

        self.calls.append((kind, payload))
        return SendResult(ok=True, detail=self.name)


async def test_sanmai_routes_by_kind_prefix() -> None:
    tg, gm, sms = _Spy("tg"), _Spy("gm"), _Spy("sms")
    n = SanMaiNotifier(telegram=tg, gmail=gm, sms=sms)

    assert (await n.send("telegram_approval", {})).detail == "tg"
    assert (await n.send("gmail_zreport", {})).detail == "gm"
    assert (await n.send("gmail", {})).detail == "gm"
    assert (await n.send("sms_status", {})).detail == "sms"
    assert tg.calls and gm.calls and sms.calls


async def test_sanmai_unknown_kind_is_failure() -> None:
    res = await SanMaiNotifier(telegram=_Spy("tg")).send("carrier_pigeon", {})
    assert res.ok is False and "unknown notify kind" in res.detail


async def test_sanmai_unconfigured_channel_is_failure() -> None:
    res = await SanMaiNotifier(telegram=_Spy("tg")).send("sms_status", {})
    assert res.ok is False and "not configured" in res.detail
