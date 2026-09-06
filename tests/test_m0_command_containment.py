"""M0 issue #122: fail-closed shell and protected-path containment."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hive.core import approval
from hive.core.types import ToolResult
from hive.tools.base import BaseTool, ToolSpec
from hive.tools.executor import DispatchStatus, ToolExecutor
from hive.tools.file_safety import build_denied_write_paths, check_path


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "command",
    [
        "rm -r -f /tmp/candidate",
        "rm -fr /",
        "git push --force origin main",
        "git reset --hard",
        "find . -name '*.py' -delete",
        "python -c \"import shutil; shutil.rmtree('/tmp/candidate')\"",
    ],
)
def test_shell_classifier_routes_known_bypasses_to_approval(command):
    assert approval.gate.is_dangerous("shell", {"cmd": command}) is True


@pytest.mark.parametrize(
    "path",
    [
        "Config/SOUL.md",
        "config/SOUL.md",
        str(REPO_ROOT / "Config" / "SOUL.md"),
        "Core/approval_gate.py",
        str(REPO_ROOT / "Core" / "approval_gate.py"),
    ],
)
def test_protected_paths_are_case_insensitive_and_normalized(path):
    assert approval.gate.is_dangerous("write_file", {"path": path}) is True


@pytest.mark.parametrize(
    "path",
    [
        REPO_ROOT / "Config" / "SOUL.md",
        REPO_ROOT / "Core" / "approval_gate.py",
        REPO_ROOT / ".git" / "config",
        REPO_ROOT / "pyproject.toml",
        REPO_ROOT / ".github" / "workflows" / "ci.yml",
    ],
)
def test_file_safety_blocks_sensitive_repo_paths_from_any_cwd(tmp_path, monkeypatch, path):
    monkeypatch.chdir(tmp_path)

    denied = build_denied_write_paths()

    assert str(path).replace("\\", "/").casefold() in denied
    assert check_path(str(path), operation="write") is not None
    assert check_path(str(path), operation="read") is not None


@pytest.mark.parametrize(
    "path",
    [
        REPO_ROOT / ".git" / "hooks" / "pre-commit",
        REPO_ROOT / ".github" / "workflows" / "release" / "publish.yml",
    ],
)
@pytest.mark.parametrize("operation", ["read", "write"])
def test_file_safety_blocks_sensitive_repo_directory_descendants(tmp_path, monkeypatch, path, operation):
    monkeypatch.chdir(tmp_path)

    assert check_path(str(path), operation=operation) is not None


@pytest.mark.parametrize(
    "path",
    [
        REPO_ROOT / ".gitignore",
        REPO_ROOT / ".github" / "workflows-backup" / "ci.yml",
    ],
)
@pytest.mark.parametrize("operation", ["read", "write"])
def test_file_safety_prefix_boundaries_do_not_block_unrelated_paths(path, operation):
    assert check_path(str(path), operation=operation) is None


@pytest.mark.parametrize(
    "command",
    [
        "git branch review-fix",
        "git diff --output=Config/SOUL.md",
        "git show HEAD:.github/workflows/ci.yml",
        "git log -p",
    ],
)
def test_content_bearing_or_mutating_git_commands_are_gated(command):
    assert approval.gate.is_dangerous("shell", {"cmd": command}) is True


@pytest.mark.parametrize(
    "command",
    [
        "git -ccore.pager=cat status",
        "git --paginate status",
        "git --git-dir=.git status",
        "git status -- .github/workflows",
    ],
)
def test_git_options_or_arguments_are_gated(command):
    assert approval.gate.is_dangerous("shell", {"cmd": command}) is True


def test_non_content_git_status_remains_safe():
    assert approval.gate.is_dangerous("shell", {"cmd": "git status"}) is False


class _Shell(BaseTool):
    spec = ToolSpec(
        name="shell",
        description="test shell",
        parameters={"type": "object", "required": ["cmd"]},
    )

    def __init__(self) -> None:
        self.executed = False

    async def execute(self, **params) -> ToolResult:
        self.executed = True
        return ToolResult(tool_name="shell", content="ran")


def test_unmatched_shell_command_is_gated_not_executed():
    shell = _Shell()
    dispatch = asyncio.run(ToolExecutor({"shell": shell}).execute(
        "shell", {"cmd": "curl https://example.invalid"}
    ))

    assert dispatch.status is DispatchStatus.PENDING
    assert shell.executed is False


@pytest.mark.parametrize(
    "command",
    [
        r"type .git\HEAD",
        r"type .github\workflows\ci.yml",
    ],
)
def test_embedded_protected_paths_are_gated_before_shell_execution(command):
    shell = _Shell()
    dispatch = asyncio.run(ToolExecutor({"shell": shell}).execute("shell", {"cmd": command}))

    assert approval.gate.is_dangerous("shell", {"cmd": command}) is True
    assert dispatch.status is DispatchStatus.PENDING
    assert shell.executed is False


def test_allowlisted_shell_command_remains_ungated():
    assert approval.gate.is_dangerous("shell", {"cmd": "echo safe"}) is False
