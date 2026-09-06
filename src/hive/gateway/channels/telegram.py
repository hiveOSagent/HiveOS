"""
telegram.py — Telegram transport (M4 #sf-2).

A thin transport over the Telegram Bot API (raw REST via httpx — no python-telegram-bot
dependency; the Bot API is a simple HTTPS interface). Parses webhook/poll updates into
MessageEvent and sends replies. No product logic lives here (OpenClaw transport-only
rule); the gateway wires inbound text to hive.ask() and sends the reply back.

The httpx client is injectable so parse/send are unit-testable without the network.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from hive.gateway.channels.base import (
    ChannelAdapter,
    MessageEvent,
    OutgoingMessage,
    SendResult,
)

log = logging.getLogger("hive.gateway.telegram")


class TelegramChannel(ChannelAdapter):
    name = "telegram"

    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None,
                 base_url: str = "https://api.telegram.org") -> None:
        self._token = token
        self._client = client or httpx.AsyncClient(timeout=30)
        self._base = f"{base_url}/bot{token}"

    def parse_update(self, update: dict[str, Any]) -> MessageEvent | None:
        # Telegram delivers the message under "message" (or "edited_message"); we only
        # act on fresh text messages.
        msg = update.get("message")
        if not isinstance(msg, dict):
            return None
        text = msg.get("text")
        chat = msg.get("chat", {})
        if not text or "id" not in chat:
            return None
        frm = msg.get("from", {}) or {}
        return MessageEvent(
            text=text,
            chat_id=str(chat["id"]),
            user_id=str(frm.get("id", "")),
            message_id=str(msg.get("message_id", "")),
            platform="telegram",
            raw=update,
        )

    async def send(self, message: OutgoingMessage) -> SendResult:
        payload: dict[str, Any] = {"chat_id": message.chat_id, "text": message.text}
        if message.reply_to:
            payload["reply_to_message_id"] = message.reply_to
        if message.reply_markup:
            payload["reply_markup"] = message.reply_markup
        try:
            r = await self._client.post(f"{self._base}/sendMessage", json=payload)
            data = r.json()
        except Exception as exc:  # noqa: BLE001 - delivery is best-effort
            log.warning("telegram send failed: %s", exc)
            return SendResult(ok=False, error=str(exc))
        if not data.get("ok"):
            return SendResult(ok=False, error=str(data.get("description", "send failed")))
        return SendResult(ok=True, message_id=str(data.get("result", {}).get("message_id", "")))

    async def answer_callback(self, callback_query_id: str, text: str) -> bool:
        """Acknowledge an inline-button press promptly, as Telegram requires."""
        try:
            response = await self._client.post(
                f"{self._base}/answerCallbackQuery",
                json={"callback_query_id": callback_query_id, "text": text[:200]},
            )
            return bool(response.json().get("ok"))
        except Exception as exc:  # noqa: BLE001 - callback acknowledgement is best-effort
            log.warning("telegram callback acknowledgement failed: %s", exc)
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
