"""M0 signed Telegram approval callbacks: replay, TTL, sender and env boundaries."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import sys

import pytest
from starlette.testclient import TestClient

from hive.core.approval import gate
from hive.core.approval_enhancements import enhance
from hive.core.config import HiveConfig
from hive.core.events import EventType
from hive.core.telegram_approvals import SIGNING_KEY_ENV, TelegramApprovalVerifier
from hive.core.self_mod import _default_run
from hive.gateway.app import create_app
from hive.gateway.channels.base import MessageEvent, OutgoingMessage, SendResult
from hive.runtime import HiveOS
from hive.tools.shell_provider import LocalShellProvider


class _Router:
    async def complete(self, *args, **kwargs):
        raise AssertionError("Telegram approval callback must never invoke the model")

    async def aclose(self):
        pass


class _Telegram:
    name = "telegram"

    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []
        self.answers: list[tuple[str, str]] = []

    def parse_update(self, update: dict) -> MessageEvent | None:
        return None

    async def send(self, message: OutgoingMessage) -> SendResult:
        self.sent.append(message)
        return SendResult(ok=True, message_id="1")

    async def answer_callback(self, callback_id: str, text: str) -> bool:
        self.answers.append((callback_id, text))
        return True


def _hive(tmp_path, monkeypatch) -> HiveOS:
    monkeypatch.setenv(SIGNING_KEY_ENV, "telegram-approval-signing-secret")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    cfg = replace(
        cfg,
        telegram_token="bot-token",
        telegram_webhook_secret="webhook-secret",
        telegram_allowed_user_ids=frozenset({"7"}),
    )
    return HiveOS.build(cfg, router=_Router())


def _callback_update(data: str, *, user_id: int = 7) -> dict:
    return {
        "callback_query": {
            "id": "callback-1",
            "from": {"id": user_id},
            "message": {"message_id": 1, "chat": {"id": user_id}},
            "data": data,
        }
    }


def test_callback_data_is_compact_signed_and_single_use(tmp_path):
    verifier = TelegramApprovalVerifier("signing-key", tmp_path / "state.sqlite")
    approve, reject = verifier.issue("approval-id")
    assert len(approve.encode("utf-8")) <= 64
    assert verifier.consume(approve) is not None
    assert verifier.consume(approve) is None
    assert verifier.consume(reject + "x") is None


def test_callback_expiry_is_rejected(tmp_path):
    now = [1000.0]
    verifier = TelegramApprovalVerifier(
        "signing-key", tmp_path / "state.sqlite", ttl_seconds=10, clock=lambda: now[0],
    )
    approve, _ = verifier.issue("approval-id")
    now[0] += 11
    assert verifier.consume(approve) is None


def test_runtime_consumes_callback_key_before_agent_build(tmp_path, monkeypatch):
    hive = _hive(tmp_path, monkeypatch)
    assert hive.telegram_approval_verifier is not None
    assert SIGNING_KEY_ENV not in __import__("os").environ
    assert not hasattr(hive.config, "telegram_approval_signing_key")


def test_child_processes_cannot_read_callback_signing_key(tmp_path, monkeypatch):
    monkeypatch.setenv(SIGNING_KEY_ENV, "telegram-approval-signing-secret")
    code = f"import os; print(os.environ.get('{SIGNING_KEY_ENV}', 'MISSING'))"
    command = f'"{sys.executable}" -c "{code}"'
    shell_result = asyncio.run(LocalShellProvider().run(command))
    self_mod_result = asyncio.run(_default_run([sys.executable, "-c", code], str(tmp_path)))
    assert shell_result.returncode == 0
    assert shell_result.stdout.strip() == "MISSING"
    assert self_mod_result[0] == 0
    assert self_mod_result[1].strip() == "MISSING"


def test_production_telegram_startup_requires_signing_key(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    cfg = replace(
        cfg,
        production_mode=True,
        secret="production-agent-secret",
        telegram_token="bot-token",
        telegram_webhook_secret="webhook-secret",
        telegram_allowed_user_ids=frozenset({"7"}),
    )
    with pytest.raises(RuntimeError, match="HIVE_TELEGRAM_APPROVAL_SIGNING_KEY"):
        HiveOS.build(cfg, router=_Router())


def test_pending_approval_is_delivered_and_callback_rejects_once(tmp_path, monkeypatch):
    hive = _hive(tmp_path, monkeypatch)
    telegram = _Telegram()
    app = create_app(hive, telegram=telegram)
    approval_id = gate.request("deploy", {"target": "prod"}, "ship")
    enhance.audit_request(approval_id)

    async def publish() -> None:
        hive.events.publish(EventType.APPROVAL_REQUESTED, {
            "approval_id": approval_id, "tool": "deploy",
        })
        await asyncio.sleep(0)

    asyncio.run(publish())
    assert len(telegram.sent) == 1
    markup = telegram.sent[0].reply_markup
    assert markup is not None
    reject_data = markup["inline_keyboard"][0][1]["callback_data"]

    with TestClient(app) as client:
        response = client.post(
            "/telegram/webhook", json=_callback_update(reject_data),
            headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
        )
        replay = client.post(
            "/telegram/webhook", json=_callback_update(reject_data),
            headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
        )
    assert response.status_code == 200
    assert response.json()["approval"]["status"] == "rejected"
    assert replay.json()["reason"] == "invalid_approval_callback"
    assert telegram.answers[0] == ("callback-1", "Decision received")


def test_callback_requires_allowlisted_user_and_does_not_consume_token(tmp_path, monkeypatch):
    hive = _hive(tmp_path, monkeypatch)
    telegram = _Telegram()
    app = create_app(hive, telegram=telegram)
    approval_id = gate.request("deploy", {}, "ship")
    enhance.audit_request(approval_id)
    _, reject = hive.telegram_approval_verifier.issue(approval_id)

    with TestClient(app) as client:
        denied = client.post(
            "/telegram/webhook", json=_callback_update(reject, user_id=8),
            headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
        )
        accepted = client.post(
            "/telegram/webhook", json=_callback_update(reject),
            headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
        )
    assert denied.json()["reason"] == "sender_not_allowed"
    assert accepted.status_code == 200
