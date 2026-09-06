"""M0 issue #121: autonomous self-modification must use a sandbox."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from hive.core.config import HiveConfig
from hive.core.sandbox import make_sandbox_runner
from hive.runtime import HiveOS


class _Router:
    async def complete(self, *args, **kwargs):  # pragma: no cover - build-only stub
        raise AssertionError("the router must not be called during construction")

    async def aclose(self):
        pass


def _autonomous_config(tmp_path, *, sandbox_image: str) -> HiveConfig:
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return replace(
        cfg,
        autonomy_enabled=True,
        autonomous_selfmod_enabled=True,
        approver_key="test-approver-key",
        sandbox_image=sandbox_image,
    )


def test_autonomous_selfmod_startup_requires_sandbox_image(tmp_path):
    cfg = _autonomous_config(tmp_path, sandbox_image="")

    with pytest.raises(RuntimeError, match="HIVE_SANDBOX_IMAGE"):
        HiveOS.build(cfg, router=_Router())


def test_config_validation_reports_missing_autonomous_selfmod_sandbox(tmp_path):
    cfg = _autonomous_config(tmp_path, sandbox_image="")

    assert any("HIVE_SANDBOX_IMAGE" in issue for issue in cfg.validate())


def test_supervised_selfmod_remains_available_without_sandbox(tmp_path):
    cfg = _autonomous_config(tmp_path, sandbox_image="")
    cfg = replace(cfg, autonomous_selfmod_enabled=False)

    hive = HiveOS.build(cfg, router=_Router())

    assert hive.self_modifier is not None


def test_autonomous_build_wires_the_configured_sandbox_runner(tmp_path, monkeypatch):
    captured = {}

    async def sandbox_runner(cmd, cwd=None):
        return 0, "ok"

    def fake_make_sandbox_runner(image, *, repo_root):
        captured["image"] = image
        captured["repo_root"] = repo_root
        return sandbox_runner

    monkeypatch.setattr("hive.runtime.make_sandbox_runner", fake_make_sandbox_runner)
    cfg = _autonomous_config(tmp_path, sandbox_image="python:3.12")

    hive = HiveOS.build(cfg, router=_Router())

    assert captured == {"image": "python:3.12", "repo_root": str(tmp_path)}
    assert hive.self_modifier._run is sandbox_runner


def test_sandbox_runner_routes_candidate_test_command_through_docker():
    seen = []

    async def local(cmd, cwd=None):
        seen.append((cmd, cwd))
        return 0, "ok"

    runner = make_sandbox_runner("python:3.12", repo_root="/candidate", base=local)
    asyncio.run(runner("python -m pytest -q", "/candidate"))

    command, cwd = seen[0]
    assert cwd == "/candidate"
    assert command.startswith("docker run --rm --network none")
    assert "-v /candidate:/repo" in command
    assert "python -m pytest -q" in command
