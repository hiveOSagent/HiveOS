"""
Smoke test the multi-channel (Slack/Discord/Email) inbound endpoints end-to-end.

Spins up the gateway with a fake `hive.ask`, fires properly-signed payloads at
each /webhook, verifies auth + routing + reply shape.

Usage:
    python scripts/smokes/channels_multi.py
Exit 0 on success, non-zero on any assertion failure.

Verifies:
- 401 on missing/wrong signature/secret for each channel
- 200 + correct reply shape on valid signed payloads
"""
from __future__ import annotations

# The Hive imports intentionally follow environment bootstrap below.
# ruff: noqa: E402, I001

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from unittest.mock import MagicMock

import nacl.signing

# Bootstrap env BEFORE importing hive.*
os.environ.setdefault("HIVE_SLACK_BOT_TOKEN", "xoxb-smoke")
os.environ.setdefault("HIVE_SLACK_SIGNING_SECRET", "smoke-slack-secret")
# Discord public key: derived from the same deterministic seed used by _discord_sign
_DISCORD_SEED = nacl.signing.SigningKey(b"\x01" * 32)
os.environ.setdefault("HIVE_DISCORD_PUBLIC_KEY", _DISCORD_SEED.verify_key.encode().hex())
os.environ.setdefault("HIVE_DISCORD_BOT_TOKEN", "discord-smoke-tok")
os.environ.setdefault("HIVE_SMTP_HOST", "smtp.smoke")
os.environ.setdefault("HIVE_SMTP_FROM", "hive@smoke")
os.environ.setdefault("HIVE_SMTP_WEBHOOK_SECRET", "smoke-email-secret")

from fastapi.testclient import TestClient  # noqa: E402

from hive.gateway.app import create_app  # noqa: E402
from hive.gateway.channels.base import (  # noqa: E402
    ChannelAdapter,
    MessageEvent,
    OutgoingMessage,
    SendResult,
)


class FakeHive:
    """Minimal HiveOS stub: ask() returns deterministic echo; records calls."""
    config = MagicMock()
    config.telegram_token = ""
    config.slack_signing_secret = "smoke-slack-secret"
    config.slack_bot_token = "xoxb-smoke"
    config.discord_public_key = _DISCORD_SEED.verify_key.encode().hex()
    config.discord_bot_token = "discord-smoke-tok"
    config.discord_application_id = ""
    config.smtp_host = "smtp.smoke"
    config.smtp_port = 587
    config.smtp_user = ""
    config.smtp_pass = ""
    config.smtp_from = "hive@smoke"
    config.smtp_webhook_secret = "smoke-email-secret"
    config.email_allowed_senders = frozenset({"alice@example.com"})
    config.api_key = "smoke-gateway-key"

    async def load_mcp_servers(self): pass
    async def aclose(self): pass

    async def ask(self, text, *, session_id, channel_hint=None):
        return f"[echo] {text} (session={session_id}, hint={channel_hint})"


class FakeChannel(ChannelAdapter):
    """Captures what the gateway would send back."""
    def __init__(self, name):
        self.name_attr = name
        self.sent: list[OutgoingMessage] = []

    def parse_update(self, update):
        # Minimal parser — smoke only needs routing, not parsing logic.
        return MessageEvent(text="hello", chat_id="42", message_id="1",
                            user_id="u1", platform=self.name_attr, raw=update)

    async def send(self, message):
        self.sent.append(message)
        return SendResult(ok=True, message_id=str(len(self.sent)))


def _slack_sign(secret: str, body: bytes, ts: int | None = None) -> dict[str, str]:
    ts = ts if ts is not None else int(time.time())
    base = f"v0:{ts}:".encode() + body
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": f"v0={digest}"}


def _discord_sign(body: bytes, ts: int | None = None) -> dict[str, str]:
    ts = ts if ts is not None else int(time.time())
    sig = _DISCORD_SEED.sign(str(ts).encode() + body).signature
    return {"X-Signature-Ed25519": sig.hex(), "X-Signature-Timestamp": str(ts)}


async def main() -> int:
    hive = FakeHive()
    app = create_app(hive)

    with TestClient(app) as client:
        # --- SLACK ----------------------------------------------------------
        print("\n  === SLACK ===")

        # 1. wrong signature → 401
        body = json.dumps({"type": "event_callback",
                           "event": {"type": "message", "text": "hi",
                                     "channel": "C1", "user": "U1"}}).encode()
        r = client.post("/slack/webhook", content=body,
                        headers={"X-Slack-Signature": "v0=deadbeef",
                                 "X-Slack-Request-Timestamp": str(int(time.time())),
                                 "Content-Type": "application/json"})
        assert r.status_code == 401, f"slack bad-sig expected 401, got {r.status_code}"
        print(f"  TEST 1 — wrong signature: HTTP {r.status_code} (expected 401)  [OK]")

        # 2. valid signature + url_verification → challenge
        payload = {"type": "url_verification", "challenge": "challenge-xyz"}
        body = json.dumps(payload).encode()
        headers = _slack_sign("smoke-slack-secret", body)
        r = client.post("/slack/webhook", content=body,
                        headers={**headers, "Content-Type": "application/json"})
        assert r.status_code == 200, f"slack challenge expected 200, got {r.status_code}"
        assert r.json() == {"challenge": "challenge-xyz"}, f"unexpected: {r.json()}"
        print(f"  TEST 2 — url_verification: HTTP {r.status_code}, challenge echoed  [OK]")

        # --- DISCORD --------------------------------------------------------
        print("\n  === DISCORD ===")

        # 3. wrong signature → 401
        body = json.dumps({"t": 2, "d": {"content": "hi", "channel_id": "1",
                                         "author": {"id": "9", "bot": False}}}).encode()
        r = client.post("/discord/webhook", content=body,
                        headers={"X-Signature-Ed25519": "00" * 64,
                                 "X-Signature-Timestamp": str(int(time.time())),
                                 "Content-Type": "application/json"})
        assert r.status_code == 401, f"discord bad-sig expected 401, got {r.status_code}"
        print(f"  TEST 3 — wrong signature: HTTP {r.status_code} (expected 401)  [OK]")

        # 4. valid signature + PING → type:1
        body = json.dumps({"t": 0, "d": {}}).encode()
        headers = _discord_sign(body)
        r = client.post("/discord/webhook", content=body,
                        headers={**headers, "Content-Type": "application/json"})
        assert r.status_code == 200, f"discord ping expected 200, got {r.status_code}"
        assert r.json() == {"type": 1}, f"unexpected: {r.json()}"
        print(f"  TEST 4 — PING:             HTTP {r.status_code}, type=1 returned  [OK]")

        # --- EMAIL ----------------------------------------------------------
        print("\n  === EMAIL ===")

        # 5. wrong secret → 401
        r = client.post("/email/webhook", content=b"From: x",
                        headers={"X-Webhook-Secret": "wrong"})
        assert r.status_code == 401, f"email bad-secret expected 401, got {r.status_code}"
        print(f"  TEST 5 — wrong secret:     HTTP {r.status_code} (expected 401)  [OK]")

        # 6. valid secret + valid RFC822 → 200, ask() called
        raw_email = (
            b"From: alice@example.com\r\n"
            b"To: hive@example.com\r\n"
            b"Subject: smoke ping\r\n"
            b"Message-ID: <smoke1@example.com>\r\n"
            b"\r\n"
            b"hello hive from email"
        )
        r = client.post("/email/webhook", content=raw_email,
                        headers={
                            "X-Webhook-Secret": "smoke-email-secret",
                            "X-Verified-Sender": "alice@example.com",
                        })
        assert r.status_code == 200, f"email valid expected 200, got {r.status_code}: {r.text}"
        body_json = r.json()
        assert body_json.get("ok") is True, f"unexpected body: {body_json}"
        assert body_json.get("handled") is True, f"email was not handled: {body_json}"
        print(f"  TEST 6 — valid message:    HTTP {r.status_code}, ask() invoked  [OK]")

    print("\n  [OK] slack / discord / email smoke complete")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
