"""M0 #147 regressions: risk tiers follow paths and verified Git changes."""
from __future__ import annotations

import asyncio

from hive.core.self_mod import SelfModifier
from hive.core.self_mod_safety import run_all_checks
from hive.core.spec_search import (
    Edit,
    EditOp,
    RiskTier,
    SelfImprovement,
    assign_tier,
    path_requires_review,
    tiered,
    validate_edit_target,
)


async def _noop(_worktree: str) -> list[str]:
    return []


def test_sensitive_path_raises_auto_edit_to_review() -> None:
    edit = Edit(
        op=EditOp.EDIT_DOCS,
        summary="misleading source edit",
        apply=_noop,
        target_files=["src\\hive\\tools\\executor.py"],
    )

    assert path_requires_review("./src/hive/tools/../tools/executor.py")
    assert path_requires_review("../outside-repo.py")
    assert path_requires_review("C:\\outside-repo.py")
    assert assign_tier(EditOp.EDIT_DOCS, edit.target_files) is RiskTier.REVIEW
    assert tiered([edit])[0].risk_tier is RiskTier.REVIEW


def test_edit_docs_rejects_non_document_target() -> None:
    reason = validate_edit_target(EditOp.EDIT_DOCS, "src/hive/tools/executor.py")

    assert reason is not None
    assert "documentation" in reason


def test_dangerous_patterns_scan_replacement_fragments_without_syntax_parse() -> None:
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


def test_callback_mismatch_with_actual_protected_change_is_blocked() -> None:
    async def apply(_worktree: str) -> list[str]:
        return ["docs/NOTES.md"]

    modifier = SelfModifier(
        repo_root="/tmp/hiveos-m0-test",
        run=_git_runner("Config/SOUL.md\n"),
    )
    result = asyncio.run(modifier.propose("test", "test", apply, dry_run=True))

    assert result["ok"] is False
    assert result["stage"] == "protected"


def test_auto_path_rejects_actual_sensitive_change() -> None:
    async def apply(_worktree: str) -> list[str]:
        return ["src/hive/core/runtime.py"]

    modifier = SelfModifier(
        repo_root="/tmp/hiveos-m0-test",
        run=_git_runner("src/hive/core/runtime.py\n"),
    )
    result = asyncio.run(modifier.propose("test", "test", apply, dry_run=True))

    assert result["ok"] is False
    assert result["stage"] == "review_required"


def test_auto_review_floor_is_reported_as_safety_block() -> None:
    class Modifier:
        async def propose(self, *_args, **_kwargs):
            return {"ok": False, "stage": "review_required", "msg": "core path"}

    async def apply(_worktree: str) -> list[str]:
        return ["tests/test_example.py"]

    outcome = asyncio.run(
        SelfImprovement(Modifier()).run(
            [Edit(op=EditOp.ADD_TEST, summary="test", apply=apply)]
        )
    )[0]

    assert outcome.status == "blocked_safety"
    assert outcome.tier is RiskTier.REVIEW


def test_post_test_git_check_blocks_new_protected_change() -> None:
    async def apply(_worktree: str) -> list[str]:
        return ["docs/NOTES.md"]

    diff_reads = 0

    async def run(cmd, cwd=None):
        nonlocal diff_reads
        command = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "git diff --name-only" in command:
            diff_reads += 1
            return 0, "docs/NOTES.md\n" if diff_reads == 1 else "Config/SOUL.md\n"
        if "git ls-files --others" in command:
            return 0, ""
        if "git rev-parse" in command:
            return 0, "deadbeef\n"
        return 0, "ok"

    modifier = SelfModifier(repo_root="/tmp/hiveos-m0-test", run=run, test_cmd="pytest -q")
    result = asyncio.run(modifier.propose("test", "test", apply, dry_run=True))

    assert result["ok"] is False
    assert result["stage"] == "protected"


def test_human_approved_review_path_allows_sensitive_change() -> None:
    async def apply(_worktree: str) -> list[str]:
        return ["src/hive/core/runtime.py"]

    modifier = SelfModifier(
        repo_root="/tmp/hiveos-m0-test",
        run=_git_runner("src/hive/core/runtime.py\n"),
    )
    result = asyncio.run(
        SelfImprovement(modifier).apply_approved(
            Edit(op=EditOp.PATCH_CODE, summary="approved", apply=apply), dry_run=True,
        )
    )

    assert result.status == "applied"
