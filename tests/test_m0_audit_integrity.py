"""M0 security slice: tamper-evident audit records and approver attribution."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from hive.core.approval_enhancements import enhance
from hive.core.config import HiveConfig
from hive.core.types import ToolCall
from hive.gateway.app import create_app
from hive.llm.adapters.base import CompletionResult
from hive.observability.audit import AuditLog
from hive.runtime import HiveOS


class _Router:
    def __init__(self, replies: list[CompletionResult] | None = None) -> None:
        self._replies = list(replies or [])

    async def complete(self, messages, kind=None, *, system=None, tools=None, **kwargs):
        return self._replies.pop(0) if self._replies else CompletionResult(text="ok", model="test")

    async def aclose(self) -> None:
        pass


def _hive(tmp_path, *, approver_key: str = "", replies: list[CompletionResult] | None = None):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(replace(cfg, approver_key=approver_key), router=_Router(replies))


def test_audit_chain_records_actor_and_principal(tmp_path):
    audit = AuditLog(tmp_path / "audit.sqlite")
    audit.record({
        "tool": "deploy", "status": "ok", "approved": True,
        "actor": "human", "principal": "human:out_of_band",
    })

    entry = audit.recent(limit=1)[0]
    assert entry["actor"] == "human"
    assert entry["principal"] == "human:out_of_band"
    assert entry["prev_digest"] == ""
    assert len(entry["digest"]) == 64
    assert audit.verify_integrity() == {"valid": True, "checked": 1, "error": None}


def test_audit_chain_detects_manual_update(tmp_path):
    path = tmp_path / "audit.sqlite"
    audit = AuditLog(path)
    audit.record({"tool": "first", "status": "ok"})
    audit.record({"tool": "second", "status": "ok"})
    audit.close()

    with sqlite3.connect(path) as db:
        db.execute("UPDATE audit_log SET status='tampered' WHERE id=1")

    check = AuditLog(path).verify_integrity()
    assert check["valid"] is False
    assert "digest" in check["error"]


def test_audit_chain_does_not_reseal_blank_digest_after_restart(tmp_path):
    path = tmp_path / "audit.sqlite"
    audit = AuditLog(path)
    audit.record({"tool": "first", "status": "ok"})
    audit.close()

    with sqlite3.connect(path) as db:
        db.execute("UPDATE audit_log SET status='tampered', digest='' WHERE id=1")

    check = AuditLog(path).verify_integrity()
    assert check["valid"] is False
    assert "digest" in check["error"]


def test_audit_chain_detects_manual_deletion(tmp_path):
    path = tmp_path / "audit.sqlite"
    audit = AuditLog(path)
    for tool in ("first", "second", "third"):
        audit.record({"tool": tool, "status": "ok"})
    audit.close()

    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM audit_log WHERE id=2")

    check = AuditLog(path).verify_integrity()
    assert check["valid"] is False
    assert "previous digest" in check["error"]


def test_audit_chain_remains_valid_for_concurrent_records(tmp_path):
    audit = AuditLog(tmp_path / "audit.sqlite")

    def record(index: int) -> None:
        audit.record({"tool": f"tool-{index}", "status": "ok"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(record, range(64)))

    assert audit.verify_integrity() == {"valid": True, "checked": 64, "error": None}


def test_audit_readers_wait_for_an_inflight_record(tmp_path, monkeypatch):
    """Every public reader must share the writer lock and avoid partial rows."""
    audit = AuditLog(tmp_path / "audit.sqlite")
    entered = threading.Event()
    release = threading.Event()
    original_digest = AuditLog._row_digest

    def slow_digest(**kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return original_digest(**kwargs)

    monkeypatch.setattr(AuditLog, "_row_digest", staticmethod(slow_digest))
    with ThreadPoolExecutor(max_workers=9) as pool:
        writer = pool.submit(audit.record, {"tool": "in-flight", "status": "ok"})
        assert entered.wait(timeout=2)
        readers = [
            pool.submit(reader)
            for reader in (
                audit.recent, audit.export, audit.stats, audit.search,
                audit.error_rate, audit.recent_errors,
                lambda: audit.recent_by_tool("in-flight"), audit.count,
            )
        ]
        assert not any(reader.done() for reader in readers)
        release.set()
        writer.result(timeout=2)
        results = [reader.result(timeout=2) for reader in readers]

    assert results[0][0]["digest"]
    assert results[6][0]["digest"]
    assert audit.verify_integrity() == {"valid": True, "checked": 1, "error": None}


def test_gateway_lifecycle_rejects_new_acquire_during_final_shutdown(tmp_path):
    hive = _hive(tmp_path)
    hive.acquire_gateway_lifespan()
    assert hive.release_gateway_lifespan() is True
    assert hive.begin_gateway_shutdown() is True
    with pytest.raises(RuntimeError, match="shutting down or closed"):
        hive.acquire_gateway_lifespan()


def test_gateway_release_does_not_close_an_injected_runtime(tmp_path):
    hive = _hive(tmp_path)
    with TestClient(create_app(hive)) as first:
        assert first.get("/health").status_code == 200
    with TestClient(create_app(hive)) as second:
        assert second.get("/health").status_code == 200


def test_gateway_startup_failure_releases_its_lifespan(tmp_path, monkeypatch):
    hive = _hive(tmp_path)

    async def fail_to_load_mcp(_self) -> None:
        raise RuntimeError("MCP startup failed")

    monkeypatch.setattr(HiveOS, "load_mcp_servers", fail_to_load_mcp)
    with pytest.raises(RuntimeError, match="MCP startup failed"):
        with TestClient(create_app(hive)):
            pass

    hive.acquire_gateway_lifespan()
    assert hive.release_gateway_lifespan() is True


def test_runtime_close_is_idempotent(tmp_path):
    hive = _hive(tmp_path)
    asyncio.run(hive.aclose())
    asyncio.run(hive.aclose())


def test_audit_chain_serializes_writers_across_instances(tmp_path):
    path = tmp_path / "audit.sqlite"
    audits = [AuditLog(path), AuditLog(path)]

    def record(index: int) -> None:
        audits[index % 2].record({"tool": f"tool-{index}", "status": "ok"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(record, range(32)))

    assert AuditLog(path).verify_integrity() == {
        "valid": True, "checked": 32, "error": None,
    }
    for audit in audits:
        audit.close()


def test_audit_verify_endpoint_requires_normal_agent_token(tmp_path):
    hive = _hive(tmp_path)
    hive.audit_log.record({"tool": "ping", "status": "ok"})
    with TestClient(create_app(hive)) as client:
        response = client.get("/audit/verify", headers={"X-Hive-Token": "change_me"})
    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_audit_purge_requires_approver_credential(tmp_path):
    hive = _hive(tmp_path, approver_key="approver-secret")
    with TestClient(create_app(hive)) as client:
        normal = client.delete("/audit/purge", headers={"X-Hive-Token": "change_me"})
        approver = client.delete(
            "/audit/purge", headers={"X-Hive-Token": "approver-secret"}
        )
    assert normal.status_code == 401
    assert approver.status_code == 200


def test_approval_history_attributes_out_of_band_approver(tmp_path):
    call = ToolCall(id="c1", name="deploy", arguments='{"target": "prod"}')
    hive = _hive(
        tmp_path,
        approver_key="approver-secret",
        replies=[CompletionResult(text="", model="test", tool_calls=[call])],
    )
    enhance.clear_history()
    with TestClient(create_app(hive)) as client:
        client.post("/chat", json={"message": "ship it"},
                    headers={"X-Hive-Token": "change_me"})
        pending = client.get("/approvals", headers={"X-Hive-Token": "change_me"}).json()
        approval_id = pending["pending"][0]["id"]
        response = client.post(
            "/approvals/decide",
            json={"approval_id": approval_id, "approved": False},
            headers={"X-Hive-Token": "approver-secret"},
        )
        entry = hive.audit_log.recent_by_tool("approval_decision", limit=1)[0]
        assert entry["actor"] == "human"
        assert entry["principal"] == "human:out_of_band"
        assert entry["status"] == "rejected"
        assert hive.audit_log.verify_integrity()["valid"] is True
    assert response.status_code == 200
    assert enhance.history(limit=1)[0].decided_by == "human:out_of_band"
    enhance.clear_history()


def test_approval_history_attributes_supervised_fallback(tmp_path):
    call = ToolCall(id="c1", name="deploy", arguments='{"target": "prod"}')
    hive = _hive(
        tmp_path,
        replies=[CompletionResult(text="", model="test", tool_calls=[call])],
    )
    enhance.clear_history()
    with TestClient(create_app(hive)) as client:
        client.post("/chat", json={"message": "ship it"},
                    headers={"X-Hive-Token": "change_me"})
        pending = client.get("/approvals", headers={"X-Hive-Token": "change_me"}).json()
        response = client.post(
            "/approvals/decide",
            json={"approval_id": pending["pending"][0]["id"], "approved": False},
            headers={"X-Hive-Token": "change_me"},
        )
        entry = hive.audit_log.recent_by_tool("approval_decision", limit=1)[0]
        assert entry["principal"] == "human:supervised_fallback"
    assert response.status_code == 200
    assert enhance.history(limit=1)[0].decided_by == "human:supervised_fallback"
    enhance.clear_history()
