"""``GmailNotifier`` — real Gmail-API notifier (accountant / Z-report email).

Self-contained: builds a Gmail client from a stored OAuth refresh-token blob and calls
``users().messages().send`` directly (no ``app.services.*``). The ``gmail.modify`` scope
covers sending. The Google SDK is a BLOCKING sync client, so the send runs in
``asyncio.to_thread`` to satisfy the async :class:`Notifier` contract.

Best-effort: any failure (no recipients, bad OAuth JSON, SDK/auth/send error) returns
``SendResult(ok=False, ...)`` — ``send`` never raises.

Importing this module needs no SDK and no creds: the Google imports are LAZY (inside the
send path) so notify stays importable on deploys that don't use Gmail. A
``service_factory`` can be injected so tests never touch the SDK or a network.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from be.adapters.notify.base import Notifier
from be.adapters.types import SendResult

_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class GmailNotifier(Notifier):
    """Real Gmail-API notifier — handles ``gmail_*`` kinds."""

    def __init__(
        self,
        *,
        oauth_json: str,
        sender: str | None = None,
        service_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._oauth_json = oauth_json
        self._sender = sender
        # Injectable so tests supply a fake Gmail service (no SDK / creds / network).
        self._service_factory = service_factory or self._build_service

    async def send(self, kind: str, payload: dict) -> SendResult:
        try:
            return await asyncio.to_thread(self._send_blocking, payload or {})
        except Exception as exc:  # noqa: BLE001 — Notifier.send must NEVER raise
            return SendResult(ok=False, detail=str(exc))

    def _send_blocking(self, p: dict) -> SendResult:
        recipients = [a for a in (p.get("to") or []) if a and a.strip()]
        if not recipients:
            return SendResult(ok=False, detail="gmail payload requires recipients")
        body = _build_mime(
            to=recipients,
            subject=p.get("subject", ""),
            html_body=p.get("html_body", ""),
            attachments=p.get("attachments"),
            sender=self._sender,
        )
        service = self._service_factory()
        resp = service.users().messages().send(userId="me", body=body).execute()
        return SendResult(ok=True, detail=str((resp or {}).get("id", "")))

    def _build_service(self) -> Any:
        # Lazy vendor import — keeps this module importable without the Google SDK.
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        cfg = json.loads(self._oauth_json)
        creds = Credentials(
            token=None,
            refresh_token=cfg["refresh_token"],
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            token_uri="https://oauth2.googleapis.com/token",
            scopes=_GMAIL_SCOPES,
        )
        return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _build_mime(
    *,
    to: list[str],
    subject: str,
    html_body: str,
    attachments: list[dict] | None,
    sender: str | None,
) -> dict:
    """Build a base64url-encoded MIME multipart message for ``messages.send``."""
    msg = MIMEMultipart("mixed")
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    if sender:
        msg["From"] = sender

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)

    for att in attachments or []:
        content = att.get("content")
        if content is None:
            continue
        filename = att.get("filename") or "attachment"
        mimetype = att.get("mimetype") or "application/octet-stream"
        _, _, subtype = mimetype.partition("/")
        part = MIMEApplication(content, _subtype=subtype or "octet-stream")
        part.set_type(mimetype if "/" in mimetype else "application/octet-stream")
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    return {"raw": raw}


__all__ = ["GmailNotifier"]
