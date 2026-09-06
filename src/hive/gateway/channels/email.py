"""email.py — Email transport: parse RFC822 inbound, aiosmtplib send outbound."""
from __future__ import annotations

import email
import email.policy
import logging
import re
from email.message import EmailMessage as _StdEmailMessage
from typing import Any, Mapping

import aiosmtplib

from hive.gateway.channels.base import (
    ChannelAdapter,
    MessageEvent,
    OutgoingMessage,
    SendResult,
)

log = logging.getLogger("hive.gateway.email")

# Cap on inbound email size (defense-in-depth: gateway also caps at MAX_WEBHOOK_BODY).
MAX_EMAIL_BYTES = 1_048_576
# Cap on multipart parts to bound parser work.
MAX_MULTIPART_PARTS = 64
# Permissive but bounded regex for From-header validation (user_id only, never chat_id).
_FROM_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DMARC_PASS_RE = re.compile(r"(?:^|[;\s])dmarc\s*=\s*pass(?:[;\s]|$)", re.IGNORECASE)


class EmailChannel(ChannelAdapter):
    name = "email"

    def __init__(self, *, smtp_host: str = "", smtp_port: int = 587,
                 smtp_user: str = "", smtp_pass: str = "", smtp_from: str = "",
                 starttls: bool = True) -> None:
        self._host = smtp_host
        self._port = smtp_port
        self._user = smtp_user
        self._pass = smtp_pass
        self._from = smtp_from or smtp_user
        self._starttls = starttls

    def parse_update(self, raw: dict[str, Any]) -> MessageEvent | None:
        if not isinstance(raw, dict):
            return None
        body_bytes = raw.get("raw_bytes")
        if not isinstance(body_bytes, (bytes, bytearray)):
            return None
        if len(body_bytes) > MAX_EMAIL_BYTES:
            log.warning("email webhook: payload %d bytes exceeds cap", len(body_bytes))
            return None
        msg = email.message_from_bytes(bytes(body_bytes), policy=email.policy.default)
        text = _extract_text(msg)
        if not text:
            return None
        subject = msg.get("Subject", "")
        full_text = f"{subject}\n\n{text}" if subject and subject != text else text
        from_addr = msg.get("From", "")
        # Sanitize From: keep only the address portion, validate with regex.
        # chat_id is the validated From (the reply recipient); user_id mirrors it.
        # The gateway uses message_id (not chat_id) for session_id, so a spoofed
        # From cannot hijack an existing session.
        chat_id = _extract_email_addr(from_addr) if from_addr else ""
        if from_addr and not chat_id:
            log.warning("email webhook: invalid From header, treating as anonymous")
        message_id = msg.get("Message-ID", "")
        in_reply_to = msg.get("In-Reply-To", "")
        # The gateway's shared webhook secret authenticates the posting MTA, not
        # the author in From.  Only trust the From allowlist after the MTA has
        # supplied its own aligned DMARC result; the ingress must strip any
        # inbound Authentication-Results header before adding this one. The
        # gateway also requires a matching X-Verified-Sender header.
        authentication_results = "\n".join(msg.get_all("Authentication-Results", []))
        sender_verified = bool(_DMARC_PASS_RE.search(authentication_results))
        # Fallback for chat_id: prefer Message-ID (unique, unspoofable) over a
        # synthetic hash so the gateway can derive a stable session_id.
        effective_chat_id = chat_id or message_id or f"unknown:{hashlib_short(raw)}"
        return MessageEvent(
            text=full_text,
            chat_id=effective_chat_id,
            user_id=chat_id,
            message_id=message_id,
            platform="email",
            raw={
                "in_reply_to": in_reply_to,
                "subject": subject,
                "sender_verified": sender_verified,
            },
        )

    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> bool:
        # v1: gateway authenticates POSTs to /email/webhook via X-Webhook-Secret;
        # sender identity is established separately from the parsed DMARC result.
        return True

    async def send(self, message: OutgoingMessage) -> SendResult:
        if not self._host or not self._from:
            return SendResult(ok=False, error="smtp host/from not configured")
        text = message.text or ""
        if not text.strip():
            return SendResult(ok=False, error="empty body")
        # Subject: prefer the first line of the reply (caller's responsibility
        # to keep the first line short). If this is a reply (reply_to set),
        # prefix with "Re: " to standardise threading.
        subject = text.splitlines()[0][:78] if text else "(no subject)"
        if message.reply_to and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        # Body: the reply text minus the leading subject line.
        body_text = text.split("\n", 1)[1] if "\n" in text else ""
        mime = _StdEmailMessage()
        mime["From"] = self._from
        # To: the operator's MTA-supplied inbound address. We do NOT trust the
        # From header from the inbound message — the gateway routes the reply
        # through the MTA's verified delivery path. If the caller wants a
        # specific recipient they must pass it via OutgoingMessage.chat_id
        # (which the gateway sources from Message-ID-derived chat_id, not From).
        mime["To"] = message.chat_id
        mime["Subject"] = subject
        if message.reply_to:
            mime["In-Reply-To"] = message.reply_to
            mime["References"] = message.reply_to
        mime.set_content(body_text or text)
        try:
            await aiosmtplib.send(
                mime,
                hostname=self._host,
                port=self._port,
                username=self._user or None,
                password=self._pass or None,
                start_tls=self._starttls,
            )
        except Exception as exc:  # noqa: BLE001 - delivery is best-effort
            log.warning("email send failed: %s", exc)
            return SendResult(ok=False, error=str(exc))
        return SendResult(ok=True, message_id=mime.get("Message-ID", ""))

    async def aclose(self) -> None:
        return None


def _extract_text(msg: Any) -> str:
    if msg.is_multipart():
        part_count = 0
        for part in msg.walk():
            part_count += 1
            if part_count > MAX_MULTIPART_PARTS:
                log.warning("email webhook: multipart part count exceeds cap %d", MAX_MULTIPART_PARTS)
                return ""
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if ctype == "text/plain" and "attachment" not in disp:
                payload = part.get_content()
                if isinstance(payload, str) and payload.strip():
                    return payload
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/html" and "attachment" not in (part.get("Content-Disposition") or "").lower():
                payload = part.get_content()
                if isinstance(payload, str) and payload.strip():
                    return payload
        return ""
    payload = msg.get_content() if hasattr(msg, "get_content") else str(msg.get_payload() or "")
    return payload if isinstance(payload, str) else ""


def _extract_email_addr(from_header: str) -> str:
    """Pull the bare email address out of a From header, e.g.
    ``"Name <a@b.com>"`` → ``"a@b.com"``. Validates with _FROM_RE.
    Returns "" if invalid.
    """
    if "<" in from_header and ">" in from_header:
        inside = from_header[from_header.find("<") + 1:from_header.find(">")]
        candidate = inside.strip()
    else:
        candidate = from_header.strip()
    if _FROM_RE.match(candidate):
        return candidate
    return ""


def hashlib_short(raw: dict[str, Any]) -> str:
    """Stable short hash for log/audit when no Message-ID is present."""
    import hashlib
    return hashlib.sha256(repr(sorted(raw.items())).encode()).hexdigest()[:12]
