"""M0 #147 regressions: risk tiers must follow paths and real Git changes."""
from __future__ import annotations

import asyncio

from hive.core.self_mod import SelfModifier
from hive.core.self_mod_safety import run_all_checks
from hive.core.spec_search import (
    Edit,
    EditOp,
    RiskTier,
    assign_tier,
    path_requires_review,
    tiered,
    validate_edit_target,
)


async def _noop(_worktree: str) -> list[str]:
    return []


def test_sensitive_source_path_raises_auto_edit_docs_to_review():
    edit = Edit(
        op=EditOp.EDIT_DOCS,
        summary="misleading source edit",
        apply=_noop,
        target_files=["src\\hive\\tools\\executor.py"],
    )

    assert path_requires_review("./src/hive/tools/../tools/executor.py")
    assert assign_tier(EditOp.EDIT_DOCS, edit.target_files) is RiskTier.REVIEW
    assert tiered([edit])[0].risk_tier is RiskTier.REVIEW


def test_edit_docs_rejects_non_document_target():
    reason = validate_edit_target(EditOp.EDIT_DOCS, "src/hive/tools/executor.py")

    assert reason is not None
    assert "documentation" in reason


def test_dangerous_patterns_run_for_replacement_fragments_without_syntax_parse():
    edit = Edit(
        op=EditOp.PATCH_CODE,
        summary="fragment",
        apply=_noop,
        code="os.system('rm -rf /')\n",
        code_is_complete_file=False,
    )

    checks = {result.check: result for result in run_all_checks(edit, code=edit.code)}

    assert "python_syntax" not in checks
    assert checks["dangerous_patterns"].passed is False


def _git_runner(diff: str, untracked: str = ""):
    async def run(cmd, cwd=None):
        command = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "git diff --name-only" in command:
            return 0, diff
        if "git ls-files --others" in command:
            return 0, untracked
        if "git rev-parse" in command:
            return 0, "deadbeef\n"
        return 0, "ok"

    return run


def test_self_modifier_blocks_when_callback_disagrees_with_actual_protected_diff():
    async def apply(_worktree: str) -> list[str]:
        return ["docs/NOTES.md"]

    modifier = SelfModifier(
        repo_root="/tmp/hiveos-m0-test",
        run=_git_runner("Config/SOUL.md\n"),
    )
    result = asyncio.run(modifier.propose("test", "test", apply, dry_run=True))

    assert result["ok"] is False
    assert result["stage"] == "protected"


def test_self_modifier_uses_verified_git_paths_for_dry_run_result():
    async def apply(_worktree: str) -> list[str]:
        return ["docs/NOTES.md"]

    modifier = SelfModifier(
        repo_root="/tmp/hiveos-m0-test",
        run=_git_runner("docs/NOTES.md\n"),
    )
    result = asyncio.run(modifier.propose("test", "test", apply, dry_run=True))

    assert result["ok"] is True
    assert result["changed"] == ["docs/NOTES.md"]


def test_self_modifier_rejects_actual_sensitive_path_on_auto_path():
    async def apply(_worktree: str) -> list[str]:
        return ["src/hive/core/runtime.py"]

    modifier = SelfModifier(
        repo_root="/tmp/hiveos-m0-test",
        run=_git_runner("src/hive/core/runtime.py\n"),
    )
    result = asyncio.run(modifier.propose("test", "test", apply, dry_run=True))

    assert result["ok"] is False
    assert result["stage"] == "review_required"


def test_self_modifier_rechecks_git_paths_after_tests():
    async def apply(_worktree: str) -> list[str]:
        return ["docs/NOTES.md"]

    calls = 0

    async def run(cmd, cwd=None):
        nonlocal calls
        command = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "git diff --name-only" in command:
            calls += 1
            return (0, "docs/NOTES.md\n" if calls == 1 else "Config/SOUL.md\n")
        if "git ls-files --others" in command:
            return 0, ""
        if "git rev-parse" in command:
            return 0, "deadbeef\n"
        if command == "pytest -q":
            return 0, "ok"
        return 0, "ok"

    modifier = SelfModifier(
        repo_root="/tmp/hiveos-m0-test", run=run, test_cmd="pytest -q",
    )
    result = asyncio.run(modifier.propose("test", "test", apply, dry_run=True))

    assert result["ok"] is False
    assert result["stage"] == "protected"
