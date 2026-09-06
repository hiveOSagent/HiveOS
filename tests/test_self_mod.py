"""self_mod flow tests (M2 #si-3 + coverage gap T5) — fake git runner, no network."""
from __future__ import annotations

import asyncio

import pytest

from hive.core.self_mod import SelfModifier


def _runner(script=None, *, push_rc=0):
    """Fake git runner. `script` maps a command prefix -> (rc, out)."""
    calls = []

    async def run(cmd, cwd=None):
        # cmd may be a list (exec-safe) or a str (shell); normalise to str for matching.
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        calls.append(cmd_str)
        if cmd_str.startswith("git rev-parse"):
            return 0, "deadbeef\n"
        if cmd_str.startswith("git diff --name-only"):
            return 0, "src/hive/llm/pricing.py\n"
        if cmd_str.startswith("git ls-files --others"):
            return 0, ""
        if cmd_str.startswith("git push"):
            return push_rc, "push output"
        return 0, "ok"

    run.calls = calls  # type: ignore[attr-defined]
    return run


async def _apply_ok(_wt):
    return ["src/hive/llm/pricing.py"]


async def _apply_protected(_wt):
    return ["Core/approval_gate.py"]


def test_dry_run_skips_push_and_pr():
    run = _runner()
    mod = SelfModifier(repo_root="/tmp/x", run=run)
    out = asyncio.run(mod.propose("t", "d", _apply_ok, dry_run=True))
    assert out["ok"] and out["stage"] == "dry_run"
    assert not any(c.startswith("git push") for c in run.calls)


def test_protected_change_refused():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    out = asyncio.run(mod.propose("t", "d", _apply_protected))
    assert not out["ok"] and out["stage"] == "protected"


def test_push_failure_reports_not_ok():
    # Latent bug fix: a failed push must not be reported as success.
    mod = SelfModifier(repo_root="/tmp/x", run=_runner(push_rc=1))
    out = asyncio.run(mod.propose("t", "d", _apply_ok))
    assert out["ok"] is False and out["stage"] == "push"


def test_pr_opener_invoked_on_success():
    opened = {}

    async def fake_opener(branch, title, body):
        opened["branch"] = branch
        opened["title"] = title
        return f"https://github.com/x/y/pull/1"

    mod = SelfModifier(repo_root="/tmp/x", run=_runner(), open_pr=fake_opener)
    out = asyncio.run(mod.propose("my title", "my body", _apply_ok))
    assert out["ok"] and out["stage"] == "pushed"
    assert out["pr_url"] == "https://github.com/x/y/pull/1"
    assert opened["title"] == "my title" and opened["branch"].startswith("hive/auto-")


def test_pr_opener_failure_keeps_branch_but_flags_note():
    async def failing_opener(branch, title, body):
        return None  # opening failed

    mod = SelfModifier(repo_root="/tmp/x", run=_runner(), open_pr=failing_opener)
    out = asyncio.run(mod.propose("t", "d", _apply_ok))
    assert out["ok"] and out["pr_url"] is None and "failed" in out["note"]


def test_no_opener_keeps_push_only_note():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    out = asyncio.run(mod.propose("t", "d", _apply_ok))
    assert out["ok"] and "pr_url" not in out and "never merges" in out["note"]


def test_selfmod_emits_start_and_end_events():
    """SELFMOD_START and SELFMOD_END must be published on the injected EventBus."""
    from hive.core.events import EventBus, EventType
    bus = EventBus(record_history=True)
    mod = SelfModifier(repo_root="/tmp/x", run=_runner(), bus=bus)
    asyncio.run(mod.propose("t", "d", _apply_ok))
    types = [e.event_type for e in bus.history()]
    assert EventType.SELFMOD_START in types
    assert EventType.SELFMOD_END in types


def test_selfmod_end_event_carries_outcome():
    """SELFMOD_END event data must include ok, stage."""
    from hive.core.events import EventBus, EventType
    ends = []
    bus = EventBus()
    bus.subscribe(EventType.SELFMOD_END, lambda e: ends.append(e.data))
    mod = SelfModifier(repo_root="/tmp/x", run=_runner(), bus=bus)
    asyncio.run(mod.propose("t", "d", _apply_ok))
    assert ends and "ok" in ends[0] and "stage" in ends[0]


def test_github_pr_opener_noops_without_token():
    from hive.core.self_mod import github_pr_opener
    opener = github_pr_opener("", "", "")   # no creds -> no network, returns None
    assert asyncio.run(opener("branch", "t", "b")) is None


def test_empty_apply_fn_returns_no_changes():
    """apply_fn that returns [] (no file changes) should yield stage=no_changes."""
    async def _apply_empty(_wt):
        return []

    def _runner_empty_status():
        async def run(cmd, cwd=None):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            if cmd_str.startswith("git rev-parse"):
                return 0, "deadbeef\n"
            if cmd_str.startswith("git diff --name-only"):
                return 0, ""
            if cmd_str.startswith("git ls-files --others"):
                return 0, ""
            if cmd_str.startswith("git status --porcelain"):
                return 0, ""  # empty status = nothing to commit
            if cmd_str.startswith("git push"):
                return 0, "pushed"
            return 0, "ok"
        return run

    mod = SelfModifier(repo_root="/tmp/x", run=_runner_empty_status())
    out = asyncio.run(mod.propose("t", "d", _apply_empty))
    assert out["ok"] is False and out["stage"] == "no_changes"


def test_title_newlines_sanitized():
    """Newlines in the commit title must be stripped before git commit."""
    committed_titles = []

    async def _apply_ok(_wt):
        return ["src/file.py"]

    def _recording_runner():
        async def run(cmd, cwd=None):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            if cmd_str.startswith("git rev-parse"):
                return 0, "abc\n"
            if cmd_str.startswith("git diff --name-only"):
                return 0, "src/file.py\n"
            if cmd_str.startswith("git ls-files --others"):
                return 0, ""
            if "commit" in cmd_str and isinstance(cmd, list):
                # The title is the last list element after -m.
                m_idx = cmd.index("-m") if "-m" in cmd else -1
                if m_idx >= 0 and m_idx + 1 < len(cmd):
                    committed_titles.append(cmd[m_idx + 1])
            if "status --porcelain" in cmd_str:
                return 0, "M src/file.py"
            if cmd_str.startswith("git push"):
                return 0, "ok"
            return 0, "ok"
        return run

    mod = SelfModifier(repo_root="/tmp/x", run=_recording_runner())
    asyncio.run(mod.propose("title\nwith\nnewlines", "desc", _apply_ok))
    if committed_titles:
        assert "\n" not in committed_titles[0]
        assert "\r" not in committed_titles[0]


# --- proposal history ---------------------------------------------------------

def test_selfmod_history_recorded_on_success():
    """history() returns the most recent successful proposal."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    assert mod.last_result is None
    asyncio.run(mod.propose("add feature", "desc", _apply_ok, dry_run=True))
    h = mod.history()
    assert len(h) == 1
    assert h[0]["title"] == "add feature"
    assert h[0]["ok"] is True
    assert h[0]["dry_run"] is True
    assert "ts" in h[0]


def test_selfmod_history_recorded_on_failure():
    """Failed proposals (protected path) also appear in history."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("bad edit", "desc", _apply_protected, dry_run=True))
    h = mod.history()
    assert h and h[0]["ok"] is False


def test_selfmod_last_result_property():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("first", "desc", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("second", "desc", _apply_ok, dry_run=True))
    assert mod.last_result is not None
    assert mod.last_result["title"] == "second"


def test_selfmod_history_newest_first():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("a", "desc", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("b", "desc", _apply_ok, dry_run=True))
    h = mod.history()
    assert h[0]["title"] == "b" and h[1]["title"] == "a"


def test_selfmod_history_capped(monkeypatch):
    """history() never returns more than _MAX_HISTORY entries internally."""
    import hive.core.self_mod as sm
    monkeypatch.setattr(sm, "_MAX_HISTORY", 3)
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    for i in range(5):
        asyncio.run(mod.propose(f"edit-{i}", "d", _apply_ok, dry_run=True))
    # Internal list is trimmed to 3; history(limit=10) returns at most 3
    assert len(mod.history(limit=10)) == 3


def test_selfmod_recent_branches_empty_when_no_proposals():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    assert mod.recent_branches() == []


def test_selfmod_recent_branches_returns_successful_ones():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("ok1", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("ok2", "d", _apply_ok, dry_run=True))
    branches = mod.recent_branches(n=5)
    assert len(branches) == 2
    assert all(b.startswith("hive/auto-") for b in branches)


def test_selfmod_recent_branches_excludes_failures():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("ok", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("fail", "d", _apply_protected, dry_run=True))
    branches = mod.recent_branches(n=10)
    assert len(branches) == 1   # only the successful one


def test_selfmod_recent_branches_capped_by_n():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    for i in range(5):
        asyncio.run(mod.propose(f"edit-{i}", "d", _apply_ok, dry_run=True))
    assert len(mod.recent_branches(n=3)) == 3


def test_selfmod_clear_history_empty():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    assert mod.clear_history() == 0


def test_selfmod_clear_history_returns_count():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("a", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("b", "d", _apply_ok, dry_run=True))
    assert mod.clear_history() == 2
    assert mod.history() == []
    assert mod.last_result is None


def test_selfmod_proposal_count_zero_initially():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    assert mod.proposal_count() == 0


def test_selfmod_proposal_count_increments():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("a", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("b", "d", _apply_ok, dry_run=True))
    assert mod.proposal_count() == 2


def test_selfmod_proposal_count_resets_after_clear():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("a", "d", _apply_ok, dry_run=True))
    mod.clear_history()
    assert mod.proposal_count() == 0


# --- success_rate ------------------------------------------------------------

def test_selfmod_success_rate_empty():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    assert mod.success_rate() == 0.0


def test_selfmod_success_rate_all_ok():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("a", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("b", "d", _apply_ok, dry_run=True))
    assert mod.success_rate() == 1.0


def test_selfmod_success_rate_mixed():
    async def _apply_fail(wt):
        return ["Config/SOUL.md"]  # protected → fails

    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("ok", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("bad", "d", _apply_fail, dry_run=True))
    rate = mod.success_rate()
    assert 0.0 < rate < 1.0


# --- failed_proposals -------------------------------------------------------

def test_selfmod_failed_proposals_empty():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("ok", "d", _apply_ok, dry_run=True))
    assert mod.failed_proposals() == []


def test_selfmod_failed_proposals_returns_failures():
    async def _apply_protected(wt):
        return ["Config/SOUL.md"]

    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("protected", "d", _apply_protected, dry_run=True))
    asyncio.run(mod.propose("ok", "d", _apply_ok, dry_run=True))
    failures = mod.failed_proposals()
    assert len(failures) == 1
    assert failures[0].get("ok") is False


def test_selfmod_failed_proposals_limit():
    async def _apply_protected(wt):
        return ["Config/SOUL.md"]

    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    for _ in range(5):
        asyncio.run(mod.propose("p", "d", _apply_protected, dry_run=True))
    assert len(mod.failed_proposals(limit=3)) == 3


# --- proposals_by_stage ------------------------------------------------------

def test_selfmod_proposals_by_stage_empty():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    assert mod.proposals_by_stage() == {}


def test_selfmod_proposals_by_stage_groups_stages():
    async def _apply_protected(wt):
        return ["Config/SOUL.md"]

    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("ok", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("ok2", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("blocked", "d", _apply_protected, dry_run=True))
    stages = mod.proposals_by_stage()
    # ok proposals get a stage too (dry_run → no_push or similar)
    assert isinstance(stages, dict)
    assert sum(stages.values()) == 3
    # protected proposal should contribute to its stage
    assert "protected" in stages


def test_selfmod_proposals_by_stage_all_same():
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("a", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("b", "d", _apply_ok, dry_run=True))
    stages = mod.proposals_by_stage()
    assert sum(stages.values()) == 2


# --- new coverage tests -------------------------------------------------------

def test_self_modifier_propose_returns_protected_on_soul_md():
    """propose() with an apply_fn that touches Config/SOUL.md returns stage='protected'."""
    async def _apply_soul_md(_wt):
        return ["Config/SOUL.md"]

    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    out = asyncio.run(mod.propose("patch soul", "desc", _apply_soul_md))
    assert out["ok"] is False
    assert out["stage"] == "protected"


def test_self_modifier_success_rate_starts_zero():
    """A brand-new SelfModifier with no proposals has success_rate() == 0.0."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    assert mod.success_rate() == 0.0


def test_self_modifier_failed_proposals_empty_initially():
    """A brand-new SelfModifier returns [] from failed_proposals()."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    assert mod.failed_proposals() == []


def test_self_modifier_proposals_by_stage_empty_initially():
    """A brand-new SelfModifier returns a dict with no entries from proposals_by_stage()."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    stages = mod.proposals_by_stage()
    assert isinstance(stages, dict)
    # No proposals yet — every stage count must be zero (dict is empty or all zeros)
    assert all(v == 0 for v in stages.values())


# --- Additional SelfModifier tests -------------------------------------------

def test_selfmod_history_is_list():
    """history() returns a list (empty initially)."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    h = mod.history()
    assert isinstance(h, list)


def test_selfmod_recent_branches_is_list():
    """recent_branches() returns a list (empty initially)."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    branches = mod.recent_branches()
    assert isinstance(branches, list)


def test_selfmod_last_result_none_initially():
    """last_result is None when no proposals have been made."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    assert mod.last_result is None


def test_selfmod_proposal_count_zero_initially():
    """proposal_count() starts at 0."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    assert mod.proposal_count() == 0


def test_selfmod_clear_history_empties_records():
    """clear_history() resets history to empty list."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("s", "r", _apply_ok, dry_run=True))
    mod.clear_history()
    assert mod.history() == []


def test_selfmod_success_rate_after_successful_dry_run():
    """After a dry-run success, proposal_count() > 0."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("fix docs", "reason", _apply_ok, dry_run=True))
    assert mod.proposal_count() >= 1


# --- New tests: additional behavioral coverage --------------------------------

def test_selfmod_last_result_reflects_latest_proposal():
    """last_result always reflects the most recently completed propose() call."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("first edit", "desc", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("second edit", "desc", _apply_ok, dry_run=True))
    assert mod.last_result is not None
    assert mod.last_result["title"] == "second edit"


def test_selfmod_dry_run_does_not_call_push():
    """A dry_run=True proposal must never issue a git push command."""
    run = _runner()
    mod = SelfModifier(repo_root="/tmp/x", run=run)
    asyncio.run(mod.propose("no push", "desc", _apply_ok, dry_run=True))
    assert not any(c.startswith("git push") for c in run.calls)


def test_selfmod_worktree_command_is_issued():
    """propose() must issue at least one git worktree command to set up an isolated tree."""
    run = _runner()
    mod = SelfModifier(repo_root="/tmp/x", run=run)
    asyncio.run(mod.propose("test worktree", "desc", _apply_ok, dry_run=True))
    assert any("worktree" in c for c in run.calls)


def test_selfmod_success_rate_one_of_two():
    """success_rate() returns 0.5 when exactly half of proposals succeed."""
    async def _apply_protected_path(_wt):
        return ["Config/SOUL.md"]

    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("good", "desc", _apply_ok, dry_run=True))       # ok=True
    asyncio.run(mod.propose("bad", "desc", _apply_protected_path))           # ok=False
    rate = mod.success_rate()
    assert rate == 0.5


def test_selfmod_failed_proposals_newest_first():
    """failed_proposals() returns most-recent failures first."""
    async def _apply_fail(_wt):
        return ["Config/SOUL.md"]

    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("fail-a", "d", _apply_fail))
    asyncio.run(mod.propose("fail-b", "d", _apply_fail))
    failures = mod.failed_proposals()
    assert len(failures) >= 2
    assert failures[0]["title"] == "fail-b"
    assert failures[1]["title"] == "fail-a"


def test_selfmod_proposals_by_stage_includes_dry_run_stage():
    """After a successful dry-run, proposals_by_stage() contains the 'dry_run' key."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("dry", "desc", _apply_ok, dry_run=True))
    stages = mod.proposals_by_stage()
    assert "dry_run" in stages
    assert stages["dry_run"] >= 1


# --- Wave 3S additional tests ---------------------------------------------------

def test_selfmod_proposal_count_zero_initially():
    """proposal_count() returns 0 before any proposals are made."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    assert mod.proposal_count() == 0


def test_selfmod_proposal_count_increments():
    """proposal_count() increases by 1 after each proposal."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("p1", "desc", _apply_ok, dry_run=True))
    assert mod.proposal_count() == 1
    asyncio.run(mod.propose("p2", "desc", _apply_ok, dry_run=True))
    assert mod.proposal_count() == 2


def test_selfmod_history_empty_initially():
    """history() returns empty list before any proposals."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    assert mod.history() == []


def test_selfmod_history_records_proposal():
    """history() contains one entry after one proposal."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("hist-test", "description", _apply_ok, dry_run=True))
    h = mod.history()
    assert len(h) >= 1
    assert any(e.get("title") == "hist-test" for e in h)


def test_selfmod_recent_branches_returns_list():
    """recent_branches() always returns a list."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    branches = mod.recent_branches()
    assert isinstance(branches, list)


def test_selfmod_clear_history_resets():
    """clear_history() resets proposal count and history to empty."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("p", "d", _apply_ok, dry_run=True))
    assert mod.proposal_count() >= 1
    mod.clear_history()
    assert mod.proposal_count() == 0
    assert mod.history() == []


# --- Wave 4D-B additional tests (8) -------------------------------------------

def test_wave4d_history_entry_has_title_key():
    """Each history record includes a 'title' key matching the proposal title."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("wave4d-title", "desc", _apply_ok, dry_run=True))
    h = mod.history()
    assert h and h[0]["title"] == "wave4d-title"


def test_wave4d_history_entry_has_ts_key():
    """Each history record includes a 'ts' (timestamp) key that is a float."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("t", "d", _apply_ok, dry_run=True))
    h = mod.history()
    assert h and isinstance(h[0]["ts"], float)


def test_wave4d_history_entry_has_stage_key():
    """Each history record includes a 'stage' key."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("t", "d", _apply_ok, dry_run=True))
    h = mod.history()
    assert h and "stage" in h[0]


def test_wave4d_history_entry_dry_run_stage():
    """A dry-run proposal records stage='dry_run' in history."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("t", "d", _apply_ok, dry_run=True))
    h = mod.history()
    assert h and h[0]["stage"] == "dry_run"


def test_wave4d_proposal_referencing_specific_file():
    """A proposal whose apply_fn returns a non-protected file is accepted."""
    async def _apply_pricing(_wt):
        return ["src/hive/llm/pricing.py"]

    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    out = asyncio.run(mod.propose("update pricing", "desc", _apply_pricing, dry_run=True))
    assert out["ok"] is True
    assert "src/hive/llm/pricing.py" in out.get("changed", [])


def test_wave4d_failed_proposal_stage_is_protected():
    """A proposal touching a protected path records stage='protected' in history."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("bad", "d", _apply_protected))
    h = mod.history()
    assert h and h[0]["stage"] == "protected"


def test_wave4d_history_ok_field_matches_result():
    """The 'ok' field in history matches the ok value returned by propose()."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    out = asyncio.run(mod.propose("t", "d", _apply_ok, dry_run=True))
    h = mod.history()
    assert h and h[0]["ok"] == out["ok"]


def test_wave4d_history_after_two_proposals_has_two_entries():
    """history() contains exactly two entries after two proposals."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("first", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("second", "d", _apply_ok, dry_run=True))
    assert len(mod.history()) == 2


# --- Wave 4I additional tests ---------------------------------------------------

async def _apply_multiple(_wt):
    return ["src/hive/llm/pricing.py", "src/hive/core/types.py", "tests/test_new.py"]


def _runner_multiple():
    async def run(cmd, cwd=None):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "git diff --name-only" in cmd_str:
            return 0, "src/hive/llm/pricing.py\nsrc/hive/core/types.py\n"
        if "git ls-files --others" in cmd_str:
            return 0, "tests/test_new.py\n"
        if "git rev-parse" in cmd_str:
            return 0, "deadbeef\n"
        return 0, "ok"
    return run


def test_wave4i_proposal_with_multiple_files_is_accepted():
    """A proposal returning multiple non-protected files is accepted (dry_run)."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner_multiple())
    out = asyncio.run(mod.propose("multi-file patch", "desc", _apply_multiple, dry_run=True))
    assert out["ok"] is True
    assert len(out.get("changed", [])) == 3


def test_wave4i_proposal_with_multiple_files_records_history():
    """Multiple-file proposals are captured in history."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("multi-file-hist", "desc", _apply_multiple, dry_run=True))
    h = mod.history()
    assert h and h[0]["title"] == "multi-file-hist"


def test_wave4i_history_is_chronological_newest_first():
    """history() returns entries in reverse-chronological order (newest first)."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("earliest", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("middle", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("latest", "d", _apply_ok, dry_run=True))
    h = mod.history()
    titles = [e["title"] for e in h]
    assert titles[0] == "latest" and titles[-1] == "earliest"


def test_wave4i_history_has_ts_in_ascending_underlying_order():
    """Underlying timestamps increase; reversed history has decreasing ts values."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("alpha", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("beta", "d", _apply_ok, dry_run=True))
    h = mod.history()
    assert len(h) == 2
    assert h[0]["ts"] >= h[1]["ts"]


def test_wave4i_failed_proposals_returns_empty_when_only_success():
    """failed_proposals() is empty when all proposals succeeded."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("ok-only", "d", _apply_ok, dry_run=True))
    assert mod.failed_proposals() == []


def test_wave4i_recent_branches_content_matches_hive_prefix():
    """All branch names in recent_branches() start with 'hive/auto-'."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("b1", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("b2", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("b3", "d", _apply_ok, dry_run=True))
    for branch in mod.recent_branches(n=10):
        assert branch.startswith("hive/auto-")


def test_wave4i_recent_branches_does_not_include_failed_proposals():
    """recent_branches() excludes proposals that failed (ok=False)."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    asyncio.run(mod.propose("good", "d", _apply_ok, dry_run=True))
    asyncio.run(mod.propose("bad", "d", _apply_protected, dry_run=True))
    branches = mod.recent_branches(n=10)
    assert len(branches) == 1


def test_wave4i_proposal_count_equals_history_length():
    """proposal_count() always equals len(history()) when under the cap."""
    mod = SelfModifier(repo_root="/tmp/x", run=_runner())
    for title in ("x1", "x2", "x3"):
        asyncio.run(mod.propose(title, "d", _apply_ok, dry_run=True))
    assert mod.proposal_count() == len(mod.history())


# ---------------------------------------------------------------------------
# Coverage gap closure — lines 41, 48, 67-82, 99, 122-123, 261, 266
# ---------------------------------------------------------------------------


def test_default_run_uses_exec_for_list_command():
    """When cmd is a list, _default_run uses create_subprocess_exec (line 41)."""
    from hive.core.self_mod import _default_run
    rc, out = asyncio.run(_default_run(["true"], "/tmp"))
    assert rc == 0
    assert isinstance(out, str)


def test_default_run_raises_when_returncode_is_none(monkeypatch):
    """_default_run must raise if the subprocess returns no returncode (line 48)."""
    from hive.core import self_mod

    class _NoneProc:
        returncode = None
        async def communicate(self):
            return (b"", b"")

    async def fake_exec(*args, **kwargs):
        return _NoneProc()

    monkeypatch.setattr(self_mod.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(RuntimeError, match="without a return code"):
        asyncio.run(self_mod._default_run(["true"], "/tmp"))


def test_github_pr_opener_creates_pr_on_201(monkeypatch):
    """github_pr_opener → 201 response → returns html_url (lines 67-78)."""
    from hive.core.self_mod import github_pr_opener

    class _Resp:
        status_code = 201
        def json(self):
            return {"html_url": "https://github.com/x/y/pull/42"}

    class _Client:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None, headers=None):
            self.last = (url, json, headers)
            return _Resp()

    class _Httpx:
        AsyncClient = _Client
        @staticmethod
        def AsyncClient_cls():  # pragma: no cover
            return _Client

    # Use a simple shim — monkey-patch the import in self_mod
    import sys
    monkeypatch.setitem(sys.modules, "httpx", _Httpx)

    opener = github_pr_opener("tok", "hive", "HiveOS")
    url = asyncio.run(opener("branch", "title", "body"))
    assert url == "https://github.com/x/y/pull/42"


def test_github_pr_opener_returns_none_on_non_2xx(monkeypatch, caplog):
    """Non-2xx response → returns None, logs warning (lines 77-79)."""
    from hive.core.self_mod import github_pr_opener
    import logging

    class _Resp:
        status_code = 422
        text = "validation failed" * 50  # > 300 chars to test the [:300] slice

    class _Client:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None, headers=None):
            return _Resp()

    class _Httpx:
        AsyncClient = _Client

    import sys
    monkeypatch.setitem(sys.modules, "httpx", _Httpx)

    opener = github_pr_opener("tok", "hive", "HiveOS")
    with caplog.at_level(logging.WARNING, logger="hive.selfmod"):
        url = asyncio.run(opener("branch", "title", "body"))
    assert url is None
    assert any("PR open failed" in r.message for r in caplog.records)


def test_github_pr_opener_returns_none_on_exception(monkeypatch, caplog):
    """httpx exception → returns None, logs warning (lines 80-82)."""
    from hive.core.self_mod import github_pr_opener
    import logging

    class _Client:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            raise RuntimeError("network down")
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **kw):
            return None

    class _Httpx:
        AsyncClient = _Client

    import sys
    monkeypatch.setitem(sys.modules, "httpx", _Httpx)

    opener = github_pr_opener("tok", "hive", "HiveOS")
    with caplog.at_level(logging.WARNING, logger="hive.selfmod"):
        url = asyncio.run(opener("branch", "title", "body"))
    assert url is None
    assert any("PR open error" in r.message for r in caplog.records)


def test_touches_protected_matches_basename_only():
    """A path whose basename matches a protected name (case-insensitive) is blocked (line 99)."""
    from hive.core.self_mod import _touches_protected
    # "SOUL.md" → norm "soul.md" not in {"config/soul.md", "core/approval_gate.py"},
    # but basename "soul.md" IS in _PROTECTED_NAMES.
    assert _touches_protected(["SOUL.md"]) is True
    assert _touches_protected(["/some/deeply/nested/approval_gate.py"]) is True
    # Plain safe path returns False.
    assert _touches_protected(["src/hive/llm/sanitize.py"]) is False


def test_emit_swallows_bus_publish_exceptions():
    """_emit must never raise even if bus.publish throws (lines 122-123)."""
    from hive.core.events import EventBus, EventType

    class _ExplodingBus:
        def publish(self, *_a, **_kw):
            raise RuntimeError("simulated bus explosion")

    mod = SelfModifier(repo_root="/tmp/x", run=_runner(), bus=_ExplodingBus())
    # Should not raise even though bus.publish raises on every call.
    asyncio.run(mod.propose("t", "d", _apply_ok, dry_run=True))


def test_propose_logs_warning_on_worktree_cleanup_failure(caplog):
    """A failed `git worktree remove` should log a warning but not crash (line 261)."""
    import logging

    def _runner_cleanup_fail():
        async def run(cmd, cwd=None):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            if cmd_str.startswith("git rev-parse"):
                return 0, "deadbeef\n"
            if cmd_str.startswith("git diff --name-only"):
                return 0, "src/hive/llm/pricing.py\n"
            if cmd_str.startswith("git ls-files --others"):
                return 0, ""
            if cmd_str.startswith("git worktree remove"):
                return 1, "permission denied"
            return 0, "ok"
        return run

    mod = SelfModifier(repo_root="/tmp/x", run=_runner_cleanup_fail())
    with caplog.at_level(logging.WARNING, logger="hive.selfmod"):
        out = asyncio.run(mod.propose("t", "d", _apply_ok, dry_run=True))
    # dry_run path: still ok; cleanup failure is logged but not propagated.
    assert out["ok"]
    assert any("worktree cleanup failed" in r.message for r in caplog.records)


def test_propose_logs_warning_on_branch_cleanup_failure(caplog):
    """A failed `git branch -D` should log a warning (line 266)."""
    import logging

    def _runner_branch_fail():
        async def run(cmd, cwd=None):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            if cmd_str.startswith("git rev-parse"):
                return 0, "deadbeef\n"
            if cmd_str.startswith("git diff --name-only"):
                return 0, "src/hive/llm/sanitize.py\n"
            if cmd_str.startswith("git ls-files --others"):
                return 0, ""
            if cmd_str.startswith("git branch -D"):
                return 1, "branch not found"
            return 0, "ok"
        return run

    mod = SelfModifier(repo_root="/tmp/x", run=_runner_branch_fail())
    with caplog.at_level(logging.WARNING, logger="hive.selfmod"):
        # Use a non-dry-run so the branch cleanup branch (line 262-266) executes.
        async def _apply_anything(_wt):
            return ["src/hive/llm/sanitize.py"]
        out = asyncio.run(mod.propose("t", "d", _apply_anything))
    assert out["ok"]
    assert any("branch cleanup failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Remaining 3 missed lines: 44 (shell form), 204 (worktree add fail), 215 (test fail)
# ---------------------------------------------------------------------------


def test_default_run_uses_shell_for_string_command():
    """When cmd is a str, _default_run uses create_subprocess_shell (line 44)."""
    from hive.core.self_mod import _default_run
    # "true" is a shell builtin on most systems; if shell path is exercised, rc==0.
    rc, out = asyncio.run(_default_run("true", "/tmp"))
    assert rc == 0
    assert out == ""


def test_propose_reports_worktree_failure():
    """A failed `git worktree add` returns stage=worktree (line 204)."""
    def _runner_wt_fail():
        async def run(cmd, cwd=None):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            if cmd_str.startswith("git rev-parse"):
                return 0, "deadbeef\n"
            if cmd_str.startswith("git worktree add"):
                return 128, "fatal: unable to create worktree"
            return 0, "ok"
        return run

    mod = SelfModifier(repo_root="/tmp/x", run=_runner_wt_fail())
    out = asyncio.run(mod.propose("t", "d", _apply_ok))
    assert out["ok"] is False
    assert out["stage"] == "worktree"
    assert "unable to create worktree" in out["log"]


def test_propose_reports_test_failure():
    """A failing test command returns stage=test, with log tail and recorded flag (line 215)."""
    def _runner_test_fail():
        async def run(cmd, cwd=None):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            if cmd_str.startswith("git rev-parse"):
                return 0, "deadbeef\n"
            if cmd_str.startswith("git diff --name-only"):
                return 0, "src/hive/llm/pricing.py\n"
            if cmd_str.startswith("git ls-files --others"):
                return 0, ""
                # The test command is `python -m pytest -q` by default; the runner sees it raw.
            if "pytest" in cmd_str:
                return 1, "FAILED test_x.py::test_y - assert 1 == 2\n" * 50
            return 0, "ok"
        return run

    mod = SelfModifier(repo_root="/tmp/x", run=_runner_test_fail())
    out = asyncio.run(mod.propose("t", "d", _apply_ok))
    assert out["ok"] is False
    assert out["stage"] == "test"
    assert out["last_good"] == "deadbeef"
    assert out["recorded"] is True
    # log is the last 2000 chars of test_out
    assert len(out["log"]) <= 2000
    assert "FAILED" in out["log"]


# --- sweep_orphaned_worktrees (Batch I: P0 autonomy durable recovery) --------

_WORKTREE_LIST_SAMPLE = """worktree /repo
HEAD abc123
branch refs/heads/main

worktree /repo/.worktrees/hive-auto-1700000000
HEAD def456
branch refs/heads/hive/auto-1700000000

worktree /repo/.worktrees/some-feature
HEAD 789abc
branch refs/heads/feature/foo

worktree /repo/.worktrees/detached-wt
HEAD 111222
detached
"""


def test_parse_worktree_list_extracts_path_branch_pairs():
    from hive.core.self_mod import _parse_worktree_list
    pairs = _parse_worktree_list(_WORKTREE_LIST_SAMPLE)
    assert ("/repo", "main") in pairs
    assert ("/repo/.worktrees/hive-auto-1700000000", "hive/auto-1700000000") in pairs
    assert ("/repo/.worktrees/some-feature", "feature/foo") in pairs
    # Detached entries (no branch line) must be skipped, not paired with garbage.
    assert not any(p == "/repo/.worktrees/detached-wt" for p, _ in pairs)


def test_parse_worktree_list_empty_input():
    from hive.core.self_mod import _parse_worktree_list
    assert _parse_worktree_list("") == []


def _sweep_runner(list_output=_WORKTREE_LIST_SAMPLE, *, list_rc=0, remove_rc=0):
    calls = []

    async def run(cmd, cwd=None):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        calls.append(cmd_str)
        if cmd_str.startswith("git worktree list"):
            return list_rc, list_output
        if cmd_str.startswith("git worktree remove"):
            return remove_rc, "ok" if remove_rc == 0 else "worktree remove failed"
        return 0, "ok"

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_sweep_removes_only_hive_auto_worktrees():
    run = _sweep_runner()
    mod = SelfModifier(repo_root="/repo", run=run)
    out = asyncio.run(mod.sweep_orphaned_worktrees())
    assert out["removed"] == ["/repo/.worktrees/hive-auto-1700000000"]
    assert out["errors"] == []
    # The orphaned skill's worktree AND local branch were cleaned up...
    assert any("git worktree remove --force /repo/.worktrees/hive-auto-1700000000" in c
              for c in run.calls)
    assert any(c == "git branch -D hive/auto-1700000000" for c in run.calls)
    # ...but the human feature-branch worktree and main were left untouched.
    assert not any("some-feature" in c for c in run.calls if "remove" in c)
    assert not any(c == "git branch -D main" for c in run.calls)
    assert not any(c == "git branch -D feature/foo" for c in run.calls)
    # Stale metadata is pruned unconditionally at the end.
    assert any(c == "git worktree prune" for c in run.calls)


def test_sweep_is_noop_on_clean_repo():
    clean = "worktree /repo\nHEAD abc123\nbranch refs/heads/main\n"
    run = _sweep_runner(list_output=clean)
    mod = SelfModifier(repo_root="/repo", run=run)
    out = asyncio.run(mod.sweep_orphaned_worktrees())
    assert out == {"removed": [], "errors": []}
    assert not any("remove" in c for c in run.calls if c.startswith("git worktree remove"))


def test_sweep_handles_list_command_failure():
    run = _sweep_runner(list_rc=1, list_output="fatal: not a git repository")
    mod = SelfModifier(repo_root="/repo", run=run)
    out = asyncio.run(mod.sweep_orphaned_worktrees())
    assert out["removed"] == []
    assert out["errors"] and "not a git repository" in out["errors"][0]


def test_sweep_collects_remove_failures_without_raising():
    run = _sweep_runner(remove_rc=1)
    mod = SelfModifier(repo_root="/repo", run=run)
    out = asyncio.run(mod.sweep_orphaned_worktrees())
    assert out["removed"] == []
    assert len(out["errors"]) == 1
    assert "/repo/.worktrees/hive-auto-1700000000" in out["errors"][0]
    # Branch delete must not be attempted for a worktree whose removal failed.
    assert not any(c == "git branch -D hive/auto-1700000000" for c in run.calls)


def test_sweep_ignores_matching_branch_name_with_mismatched_path():
    """Defense in depth: a worktree whose branch happens to be named
    hive/auto-<x> but whose directory does NOT match the exact path
    _propose_inner would have created (e.g. a human manually named a branch
    this way) must be left alone."""
    suspicious = (
        "worktree /repo\nHEAD abc123\nbranch refs/heads/main\n\n"
        "worktree /repo/some/other/place\nHEAD def456\n"
        "branch refs/heads/hive/auto-9999999999\n"
    )
    run = _sweep_runner(list_output=suspicious)
    mod = SelfModifier(repo_root="/repo", run=run)
    out = asyncio.run(mod.sweep_orphaned_worktrees())
    assert out["removed"] == []
    assert not any("remove" in c for c in run.calls if c.startswith("git worktree remove"))
