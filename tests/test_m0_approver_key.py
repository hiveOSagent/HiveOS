"""M0 security slice: out-of-band approval credential and child-process isolation."""
from __future__ import annotations

import asyncio
import sys
from dataclasses import replace

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

from hive.core.config import HiveConfig
from hive.core.self_mod import _default_run
from hive.core.types import ToolCall
from hive.gateway.app import create_app
from hive.gateway.auth import make_approver_dependency
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS
from hive.tools.shell_provider import DockerShellProvider, LocalShellProvider


class _ScriptRouter:
    def __init__(self, script):
        self._script = list(script)

    async def complete(self, messages, kind=None, *, system=None, tools=None, **kwargs):
        item = self._script.pop(0) if self._script else CompletionResult(text="ok", model="m")
        return item if isinstance(item, CompletionResult) else CompletionResult(text=item, model="m")

    async def aclose(self):
        pass


def _hive(tmp_path, *, approver_key: str = "", script=None) -> HiveOS:
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    cfg = replace(cfg, approver_key=approver_key)
    return HiveOS.build(cfg, router=_ScriptRouter(script or []))


def _approval_hive(tmp_path, *, approver_key: str = "") -> HiveOS:
    call = ToolCall(id="c1", name="deploy", arguments='{"target": "prod"}')
    return _hive(
        tmp_path,
        approver_key=approver_key,
        script=[CompletionResult(text="", model="m", tool_calls=[call]),
                CompletionResult(text="queued", model="m")],
    )


def _python_env_command() -> str:
    code = "import os; print(os.environ.get('HIVE_APPROVER_KEY', 'MISSING'))"
    return f'"{sys.executable}" -c "{code}"'


def test_approver_dependency_accepts_only_approver_key():
    dependency = make_approver_dependency("approver-secret")
    asyncio.run(dependency(x_hive_token="approver-secret"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(dependency(x_hive_token="agent-secret"))
    assert exc.value.status_code == 401


def test_approvals_decide_rejects_normal_agent_token(tmp_path):
    hive = _approval_hive(tmp_path, approver_key="approver-secret")
    with TestClient(create_app(hive)) as client:
        client.post("/chat", json={"message": "ship it"},
                    headers={"X-Hive-Token": "change_me"})
        approval_id = client.get("/approvals", headers={"X-Hive-Token": "change_me"}).json()["pending"][0]["id"]

        response = client.post(
            "/approvals/decide",
            json={"approval_id": approval_id, "approved": True},
            headers={"X-Hive-Token": "change_me"},
        )
        assert response.status_code == 401
        assert client.get("/approvals", headers={"X-Hive-Token": "change_me"}).status_code == 200


def test_approvals_decide_accepts_approver_key(tmp_path):
    hive = _approval_hive(tmp_path, approver_key="approver-secret")
    with TestClient(create_app(hive)) as client:
        client.post("/chat", json={"message": "ship it"},
                    headers={"X-Hive-Token": "change_me"})
        approval_id = client.get("/approvals", headers={"X-Hive-Token": "change_me"}).json()["pending"][0]["id"]

        response = client.post(
            "/approvals/decide",
            json={"approval_id": approval_id, "approved": True},
            headers={"X-Hive-Token": "approver-secret"},
        )
        assert response.status_code == 200
        assert response.json()["executed"] is True


def test_supervised_mode_falls_back_to_agent_secret_with_warning(tmp_path, caplog):
    hive = _approval_hive(tmp_path)
    with caplog.at_level("WARNING", logger="hive.gateway"):
        with TestClient(create_app(hive)) as client:
            client.post("/chat", json={"message": "ship it"},
                        headers={"X-Hive-Token": "change_me"})
            approval_id = client.get("/approvals", headers={"X-Hive-Token": "change_me"}).json()["pending"][0]["id"]
            response = client.post(
                "/approvals/decide",
                json={"approval_id": approval_id, "approved": True},
                headers={"X-Hive-Token": "change_me"},
            )
    assert response.status_code == 200
    assert any("HIVE_APPROVER_KEY" in record.message and "HIVE_SECRET" in record.message
               for record in caplog.records)


def test_config_loads_and_redacts_approver_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_APPROVER_KEY", "approver-secret")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.approver_key == "approver-secret"
    safe = cfg.to_safe_dict()
    assert safe["approver_key"] == "***"
    assert "approver-secret" not in str(safe)


def test_autonomy_build_fails_closed_without_approver_key(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    cfg = replace(cfg, autonomy_enabled=True, approver_key="")
    with pytest.raises(RuntimeError, match="HIVE_APPROVER_KEY"):
        HiveOS.build(cfg, router=_ScriptRouter([]))


def test_local_shell_child_cannot_read_approver_key(monkeypatch):
    monkeypatch.setenv("HIVE_APPROVER_KEY", "approver-secret")
    result = asyncio.run(LocalShellProvider().run(_python_env_command()))
    assert result.returncode == 0
    assert result.stdout.strip() == "MISSING"


def test_docker_shell_filters_approver_key_from_args_and_child(monkeypatch):
    captured = {}
    monkeypatch.setenv("HIVE_APPROVER_KEY", "parent-secret")

    class _Process:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

    async def fake_create_subprocess_shell(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _Process()

    monkeypatch.setattr("hive.tools.shell_provider.asyncio.create_subprocess_shell",
                        fake_create_subprocess_shell)
    result = asyncio.run(DockerShellProvider().run(
        "echo ok",
        env={"HIVE_APPROVER_KEY": "approver-secret", "SAFE_VALUE": "ok"},
    ))
    assert result.stdout == "ok"
    assert "HIVE_APPROVER_KEY" not in captured["command"]
    assert "SAFE_VALUE=ok" in captured["command"]
    assert "HIVE_APPROVER_KEY" not in captured["kwargs"]["env"]


def test_self_mod_child_cannot_read_approver_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_APPROVER_KEY", "approver-secret")
    code = "import os; print(os.environ.get('HIVE_APPROVER_KEY', 'MISSING'))"
    returncode, output = asyncio.run(_default_run([sys.executable, "-c", code], str(tmp_path)))
    assert returncode == 0
    assert output.strip() == "MISSING"
