"""M0 issue #143: executable regression checks for security boundaries."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from hive.core import approval
from hive.core.self_mod import _default_run
from hive.tools.builtins import ReadFile, Shell
from hive.tools.executor import DispatchStatus, ToolExecutor
from hive.tools.file_safety import check_path
from hive.tools.shell_provider import LocalShellProvider, ShellResult


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "git merge feature/security",
        "gh pr merge 143 --squash",
    ],
)
def test_control_plane_mutations_are_approval_bound(command):
    """The real classifier must gate operations that can alter repository state."""
    assert approval.gate.is_dangerous("shell", {"cmd": command}) is True


def test_workflow_and_git_descendants_are_blocked_from_outside_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    for relative in (".git/HEAD", ".git/hooks/pre-commit", ".github/workflows/nightly.yml"):
        path = Path(__file__).resolve().parents[1] / relative
        assert check_path(str(path), operation="read") is not None
        assert check_path(str(path), operation="write") is not None


def test_behavioral_harness_uses_a_real_scratch_git_repository(tmp_path, monkeypatch):
    """Keep the control-plane check independent from the checkout's Git state."""
    repo = tmp_path / "scratch-repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--short"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == ""

    class _MarkerProvider:
        async def run(self, _cmd: str, **_kwargs) -> ShellResult:
            (repo / "executed").write_text("unexpected", encoding="utf-8")
            return ShellResult(stdout="unexpected", returncode=0)

    monkeypatch.chdir(repo)
    dispatch = asyncio.run(ToolExecutor({
        "shell": Shell(provider=_MarkerProvider()),
    }).execute("shell", {"cmd": "git push origin main"}))

    assert dispatch.status is DispatchStatus.PENDING
    assert not (repo / "executed").exists()


def test_read_file_rejects_user_credential_files(tmp_path, monkeypatch):
    import hive.tools.file_safety as file_safety

    home = tmp_path / "home"
    credential = home / ".ssh" / "id_ed25519"
    credential.parent.mkdir(parents=True)
    credential.write_text("private-key-sentinel", encoding="utf-8")
    monkeypatch.setattr(file_safety.Path, "home", classmethod(lambda cls: home))

    result = asyncio.run(ReadFile().execute(path=str(credential)))

    assert result.success is False
    assert "private-key-sentinel" not in result.content


def test_relative_credential_read_is_rejected_from_home_cwd(tmp_path, monkeypatch):
    import hive.tools.file_safety as file_safety

    home = tmp_path / "home"
    credential = home / ".ssh" / "id_rsa"
    credential.parent.mkdir(parents=True)
    credential.write_text("private-key-sentinel", encoding="utf-8")
    monkeypatch.setattr(file_safety.Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(home)

    assert check_path(".ssh/id_rsa", operation="read") is not None


def test_executor_rejects_credential_read_before_tool_execution(tmp_path, monkeypatch):
    import hive.tools.file_safety as file_safety

    home = tmp_path / "home"
    credential = home / ".netrc"
    home.mkdir()
    credential.write_text("machine example login user password secret", encoding="utf-8")
    monkeypatch.setattr(file_safety.Path, "home", classmethod(lambda cls: home))

    dispatch = asyncio.run(ToolExecutor({"read_file": ReadFile()}).execute(
        "read_file", {"path": str(credential)}
    ))

    assert dispatch.status is DispatchStatus.ERROR
    assert "secret" not in (dispatch.error or "")


def test_read_file_traversal_remains_blocked(tmp_path, monkeypatch):
    import hive.tools.file_safety as file_safety

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(file_safety.Path, "home", classmethod(lambda cls: tmp_path))

    dispatch = asyncio.run(ToolExecutor({"read_file": ReadFile()}).execute(
        "read_file", {"path": "safe/../outside.txt"}
    ))

    assert dispatch.status is DispatchStatus.ERROR
    assert "traversal" in (dispatch.error or "")


def _env_probe() -> str:
    code = "import os; print(os.environ.get('HIVE_APPROVER_KEY', 'MISSING'))"
    return f'"{sys.executable}" -c "{code}"'


def test_shell_child_cannot_read_approver_credential(monkeypatch):
    monkeypatch.setenv("HIVE_APPROVER_KEY", "approver-sentinel")

    result = asyncio.run(LocalShellProvider().run(_env_probe()))

    assert result.returncode == 0
    assert result.stdout.strip() == "MISSING"


def test_self_mod_child_cannot_read_approver_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_APPROVER_KEY", "approver-sentinel")
    code = "import os; print(os.environ.get('HIVE_APPROVER_KEY', 'MISSING'))"

    returncode, output = asyncio.run(_default_run(
        [sys.executable, "-c", code], str(tmp_path)
    ))

    assert returncode == 0
    assert output.strip() == "MISSING"
