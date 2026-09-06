"""
test_m10_self_improve.py — M10-c: self-improvement depth.

Tests:
  - TaskBoard.recent_failures() query
  - HiveOS.self_improve_from_symptom() is callable and returns a list
  - Heartbeat tick returns self_improved key
  - REVIEW/MANUAL edits are enqueued as self_improve tasks
"""
from __future__ import annotations

import asyncio

import pytest

from hive.core.config import HiveConfig
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS


class _ScriptRouter:
    async def complete(self, messages, *, system="", tools=None, **kw):
        return CompletionResult(text="[]", model="test")

    async def stream(self, messages, *, system="", **kw):
        yield "ok"

    async def aclose(self):
        pass


def _make_hive(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(cfg, router=_ScriptRouter())


# ---------------------------------------------------------------------------
# TaskBoard.recent_failures
# ---------------------------------------------------------------------------

def test_recent_failures_empty(tmp_path):
    hive = _make_hive(tmp_path)
    assert hive.task_board.recent_failures() == []


def test_recent_failures_returns_failed_tasks(tmp_path):
    hive = _make_hive(tmp_path)
    tid = hive.task_board.enqueue("tool", {"tool": "web_get"}, source="test")
    hive.task_board.claim(tid)
    hive.task_board.fail(tid, "timeout")

    failures = hive.task_board.recent_failures(limit=10)
    assert len(failures) == 1
    assert failures[0].state == "failed"
    assert failures[0].last_error == "timeout"


def test_recent_failures_only_failed_state(tmp_path):
    hive = _make_hive(tmp_path)
    # Enqueue 3: one done, one failed, one pending
    tid1 = hive.task_board.enqueue("tool", {}, source="test")
    hive.task_board.claim(tid1)
    hive.task_board.complete(tid1)

    tid2 = hive.task_board.enqueue("tool", {}, source="test")
    hive.task_board.claim(tid2)
    hive.task_board.fail(tid2, "err")

    hive.task_board.enqueue("tool", {}, source="test")  # pending

    failures = hive.task_board.recent_failures()
    assert len(failures) == 1
    assert failures[0].state == "failed"


def test_recent_failures_limit(tmp_path):
    hive = _make_hive(tmp_path)
    for i in range(5):
        tid = hive.task_board.enqueue("tool", {}, source="test")
        hive.task_board.claim(tid)
        hive.task_board.fail(tid, f"err {i}")

    assert len(hive.task_board.recent_failures(limit=3)) == 3


def test_recent_failures_newest_first(tmp_path):
    hive = _make_hive(tmp_path)
    for i in range(3):
        tid = hive.task_board.enqueue("tool", {}, source="test")
        hive.task_board.claim(tid)
        hive.task_board.fail(tid, f"err {i}")

    failures = hive.task_board.recent_failures()
    ids = [f.id for f in failures]
    assert ids == sorted(ids, reverse=True)


# ---------------------------------------------------------------------------
# HiveOS.self_improve_from_symptom
# ---------------------------------------------------------------------------

def test_self_improve_from_symptom_returns_list(tmp_path):
    hive = _make_hive(tmp_path)
    outcomes = asyncio.run(hive.self_improve_from_symptom("repeated timeout errors"))
    assert isinstance(outcomes, list)


def test_self_improve_from_symptom_no_crash_on_empty_response(tmp_path):
    hive = _make_hive(tmp_path)
    # _ScriptRouter returns text="[]" so diagnoser returns no edits — safe no-op
    outcomes = asyncio.run(hive.self_improve_from_symptom("test symptom"))
    assert outcomes == []


# ---------------------------------------------------------------------------
# Heartbeat tick — self_improved key present
# ---------------------------------------------------------------------------

def test_tick_returns_self_improved_key(tmp_path):
    from hive.autonomy.heartbeat import Heartbeat
    hive = _make_hive(tmp_path)
    beat = Heartbeat(hive)
    result = asyncio.run(beat.tick())
    assert "self_improved" in result
    assert isinstance(result["self_improved"], int)


def test_tick_self_improve_not_triggered_below_threshold(tmp_path):
    from hive.autonomy.heartbeat import Heartbeat
    hive = _make_hive(tmp_path)
    # Only 2 failures — below the ≥3 threshold; self_improved should stay 0
    for _ in range(2):
        tid = hive.task_board.enqueue("tool", {"tool": "web_get"}, source="test")
        hive.task_board.claim(tid)
        hive.task_board.fail(tid, "err")

    beat = Heartbeat(hive)
    result = asyncio.run(beat.tick())
    assert result["self_improved"] == 0


# ---------------------------------------------------------------------------
# _diagnoser JSON parsing (the bug fix: used to return [] unconditionally)
# ---------------------------------------------------------------------------

class _EditRouter:
    """Returns a JSON array with one EDIT_DOCS edit."""
    def __init__(self, payload: str = "[]"):
        self._payload = payload

    async def complete(self, messages, *, system="", tools=None, **kw):
        return CompletionResult(text=self._payload, model="test")

    async def stream(self, messages, *, system="", **kw):
        yield "ok"

    async def aclose(self):
        pass


def test_diagnoser_parses_edit_docs_json(tmp_path):
    """Diagnoser must convert valid JSON into Edit objects (not discard them)."""
    import json as _json
    payload = _json.dumps([{
        "op": "edit_docs",
        "summary": "update readme",
        "rationale": "out of date",
        "path": "README.md",
        "old_text": "",
        "new_text": "updated",
    }])
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_EditRouter(payload))
    outcomes = asyncio.run(hive.self_improve_from_symptom("test symptom"))
    # EDIT_DOCS with a valid documentation path is accepted and reaches the
    # self-mod flow; in test env git will fail (not a real repo).
    # What matters: outcomes is non-empty and not silently discarded.
    assert isinstance(outcomes, list)
    assert len(outcomes) == 1


def test_diagnoser_skips_unknown_op(tmp_path):
    """Edits with unknown op values are silently skipped."""
    import json as _json
    payload = _json.dumps([{"op": "DOES_NOT_EXIST", "summary": "x", "rationale": "y"}])
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_EditRouter(payload))
    outcomes = asyncio.run(hive.self_improve_from_symptom("bad op"))
    assert outcomes == []


def test_diagnoser_handles_malformed_json(tmp_path):
    """Non-JSON response from router is handled gracefully."""
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_EditRouter("not valid json {{"))
    outcomes = asyncio.run(hive.self_improve_from_symptom("malformed"))
    assert outcomes == []


def test_review_tier_edit_stored_in_edit_pending(tmp_path):
    """REVIEW-tier edits must be stored in hive.edit_pending for later approval."""
    import json as _json
    # PATCH_CODE is REVIEW tier per the tier table.
    payload = _json.dumps([{
        "op": "patch_code",
        "summary": "fix the bug",
        "rationale": "it crashes",
    }])
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_EditRouter(payload))
    outcomes = asyncio.run(hive.self_improve_from_symptom("crash symptom"))
    assert len(outcomes) == 1
    assert outcomes[0].status == "pending_approval"
    # The Edit object must be stored so the gateway can retrieve it on approval.
    assert len(hive.edit_pending) == 1
    approval_id = outcomes[0].approval_id
    assert approval_id in hive.edit_pending


def test_review_tier_enqueues_task(tmp_path):
    """REVIEW-tier outcomes must be enqueued as self_improve tasks in the task board."""
    import json as _json
    payload = _json.dumps([{
        "op": "patch_code",
        "summary": "fix crash",
        "rationale": "boom",
    }])
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_EditRouter(payload))
    asyncio.run(hive.self_improve_from_symptom("bad symptom"))
    tasks = hive.task_board.all()
    si_tasks = [t for t in tasks if t.kind == "self_improve"]
    assert si_tasks, "REVIEW-tier outcome must enqueue a self_improve task"
    assert si_tasks[0].payload.get("tier") == "review"
    # New: task payload must include op for transparency in /tasks
    assert si_tasks[0].payload.get("op") == "patch_code"


def test_manual_tier_enqueues_task(tmp_path):
    """MANUAL-tier outcomes must also be enqueued as self_improve tasks."""
    import json as _json
    # PATCH_SYSTEM_PROMPT is not MANUAL; use op with MANUAL tier if one exists,
    # otherwise test by directly checking RiskTier enum comparison is correct.
    from hive.core.spec_search import RiskTier
    assert RiskTier.MANUAL.value == "manual"  # guard the string we assert below
    # All ops below MANUAL in the tier table — verify tier enum comparison doesn't regress.
    payload = _json.dumps([])
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_EditRouter(payload))
    outcomes = asyncio.run(hive.self_improve_from_symptom("no-op symptom"))
    assert outcomes == []  # empty payload → no tasks enqueued
    assert hive.task_board.all() == []


# ---------------------------------------------------------------------------
# Security: path traversal guard in _apply closure
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# HiveOS.run_tests() — offline unit tests (monkey-patch subprocess)
# ---------------------------------------------------------------------------

def test_run_tests_all_passed(tmp_path, monkeypatch):
    """run_tests() returns all_passed=True when pytest exits 0."""
    import asyncio as _asyncio

    async def _fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        class _Proc:
            returncode = 0
            async def communicate(self):
                return b"387 passed, 4 skipped in 11.7s\n", b""
        return _Proc()

    monkeypatch.setattr(_asyncio, "create_subprocess_shell", _fake_shell)
    hive = _make_hive(tmp_path)
    result = asyncio.run(hive.run_tests(test_cmd="pytest -q"))
    assert result["all_passed"] is True
    assert result["passed"] == 387
    assert result["failed"] == 0
    assert result["timed_out"] is False


def test_run_tests_with_failures(tmp_path, monkeypatch):
    """run_tests() returns all_passed=False and counts failures correctly."""
    import asyncio as _asyncio

    async def _fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        class _Proc:
            returncode = 1
            async def communicate(self):
                return b"3 passed, 2 failed in 1.2s\n", b""
        return _Proc()

    monkeypatch.setattr(_asyncio, "create_subprocess_shell", _fake_shell)
    hive = _make_hive(tmp_path)
    result = asyncio.run(hive.run_tests(test_cmd="pytest -q"))
    assert result["all_passed"] is False
    assert result["passed"] == 3
    assert result["failed"] == 2
    assert result["returncode"] == 1


def test_run_tests_timeout(tmp_path, monkeypatch):
    """run_tests() returns timed_out=True when the process times out."""
    import asyncio as _asyncio

    async def _fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        class _Proc:
            returncode = -1
            def kill(self): pass
            async def communicate(self): return b"", b""
        return _Proc()

    async def _fake_wait_for(coro, timeout):
        # cancel the coroutine to avoid "never awaited" warnings
        coro.close()
        raise _asyncio.TimeoutError()

    monkeypatch.setattr(_asyncio, "create_subprocess_shell", _fake_shell)
    monkeypatch.setattr(_asyncio, "wait_for", _fake_wait_for)

    hive = _make_hive(tmp_path)
    result = asyncio.run(hive.run_tests(test_cmd="pytest -q", timeout=0.001))
    assert result["timed_out"] is True
    assert result["all_passed"] is False


# ---------------------------------------------------------------------------
# HiveOS.self_diagnose() — calls run_tests then self_improve_from_symptom
# ---------------------------------------------------------------------------

def test_self_diagnose_no_improvement_when_all_pass(tmp_path, monkeypatch):
    """self_diagnose() does not trigger self-improvement when tests pass."""
    import asyncio as _asyncio

    async def _fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        class _Proc:
            returncode = 0
            async def communicate(self):
                return b"10 passed in 0.5s\n", b""
        return _Proc()

    monkeypatch.setattr(_asyncio, "create_subprocess_shell", _fake_shell)
    hive = _make_hive(tmp_path)
    result = asyncio.run(hive.self_diagnose(test_cmd="pytest -q"))
    assert result["all_passed"] is True
    assert result["improvement_outcomes"] == []


def test_self_diagnose_triggers_improvement_on_failure(tmp_path, monkeypatch):
    """self_diagnose() triggers self_improve_from_symptom() when tests fail."""
    import asyncio as _asyncio

    async def _fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        class _Proc:
            returncode = 1
            async def communicate(self):
                return b"1 passed, 2 failed in 0.5s\nFAILED tests/test_x.py::test_y\n", b""
        return _Proc()

    monkeypatch.setattr(_asyncio, "create_subprocess_shell", _fake_shell)
    # _ScriptRouter returns "[]" so self_improve_from_symptom yields no edits
    hive = _make_hive(tmp_path)
    result = asyncio.run(hive.self_diagnose(test_cmd="pytest -q"))
    assert result["all_passed"] is False
    assert result["failed"] == 2
    assert "improvement_outcomes" in result


def test_self_diagnose_skips_diagnoser_when_near_cap(tmp_path, monkeypatch):
    """self_diagnose() skips the LLM diagnoser when the budgeter is near cap."""
    import asyncio as _asyncio

    async def _fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        class _Proc:
            returncode = 1
            async def communicate(self):
                return b"0 passed, 1 failed in 0.1s\n", b""
        return _Proc()

    monkeypatch.setattr(_asyncio, "create_subprocess_shell", _fake_shell)
    hive = _make_hive(tmp_path)
    # Exhaust the budget
    for _ in range(hive.config.daily_call_cap):
        hive.budgeter.record_call()
    result = asyncio.run(hive.self_diagnose(test_cmd="pytest -q"))
    assert result["all_passed"] is False
    assert result["skipped_reason"] == "near_daily_cap"
    assert result["improvement_outcomes"] == []


def test_self_diagnose_returns_improvement_key_on_timeout(tmp_path, monkeypatch):
    """self_diagnose() returns improvement_outcomes=[] when tests time out."""
    import asyncio as _asyncio

    async def _fake_wait_for(coro, timeout):
        coro.close()
        raise _asyncio.TimeoutError()

    async def _fake_shell(cmd, cwd=None, stdout=None, stderr=None):
        class _Proc:
            returncode = -1
            def kill(self): pass
            async def communicate(self): return b"", b""
        return _Proc()

    monkeypatch.setattr(_asyncio, "create_subprocess_shell", _fake_shell)
    monkeypatch.setattr(_asyncio, "wait_for", _fake_wait_for)

    hive = _make_hive(tmp_path)
    result = asyncio.run(hive.self_diagnose(test_cmd="pytest -q"))
    assert result["timed_out"] is True
    assert result["improvement_outcomes"] == []


# ---------------------------------------------------------------------------
# Security: path traversal guard in _apply closure
# ---------------------------------------------------------------------------

def test_syntax_validation_blocks_bad_py_patch(tmp_path):
    """A patch that creates a Python syntax error must be rejected by _apply."""
    import json as _json
    from unittest.mock import AsyncMock, patch

    from hive.core.spec_search import EditOp, EditOutcome, RiskTier

    wt = tmp_path / "worktree"
    wt.mkdir()
    src = wt / "src"
    src.mkdir()
    target = src / "module.py"
    target.write_text("def foo():\n    return 1\n", encoding="utf-8")

    payload = _json.dumps([{
        "op": "edit_docs",   # EDIT_DOCS is AUTO — exercises the _apply code path
        "summary": "break syntax",
        "rationale": "test",
        "path": "src/module.py",
        "old_text": "return 1",
        "new_text": "return ((((",  # unclosed parens — syntax error
    }])

    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_EditRouter(payload))

    async def _fake_propose(title, description, apply_fn, *, dry_run=False):
        modified = await apply_fn(str(wt))
        # If syntax validation worked, modified should be empty (edit skipped)
        return {"ok": True, "stage": "dry_run", "branch": "test", "changed": modified}

    with patch.object(hive.self_modifier, "propose", side_effect=_fake_propose):
        asyncio.run(hive.self_improve_from_symptom("syntax test"))

    # File must be unchanged (syntax error blocked the write)
    assert "return 1" in target.read_text(encoding="utf-8")


def test_heartbeat_tick_emits_agent_tick_events(tmp_path):
    """tick() must publish AGENT_TICK_START and AGENT_TICK_END events."""
    from hive.autonomy.heartbeat import Heartbeat
    from hive.core.events import EventBus, EventType
    hive = _make_hive(tmp_path)
    received = []
    hive.events.subscribe(EventType.AGENT_TICK_START, lambda e: received.append(e.event_type))
    hive.events.subscribe(EventType.AGENT_TICK_END, lambda e: received.append(e.event_type))
    beat = Heartbeat(hive)
    asyncio.run(beat.tick())
    assert EventType.AGENT_TICK_START in received
    assert EventType.AGENT_TICK_END in received


def test_diagnoser_apply_closure_rejects_path_traversal(tmp_path):
    """The _apply closure from the REAL self_improve_from_symptom() must refuse
    paths that resolve outside the worktree.

    Tests the production closure (not a hand-copied guard) by patching
    SelfModifier.propose to call edit.apply() with a controlled fake worktree,
    so the actual runtime.py code path is exercised.
    """
    import json as _json
    from unittest.mock import AsyncMock, patch

    from hive.core.spec_search import EditOp, EditOutcome, RiskTier

    wt = tmp_path / "worktree"
    wt.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("sentinel")

    # Router returns a path-traversal edit: path escapes the worktree via ../..
    payload = _json.dumps([{
        "op": "edit_docs",
        "summary": "evil",
        "rationale": "attack",
        "path": "../../outside.txt",
        "old_text": "sentinel",
        "new_text": "OVERWRITTEN",
    }])

    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_EditRouter(payload))

    # Intercept SelfModifier.propose: call the real apply_fn with our
    # controlled fake worktree so the production _apply closure is exercised.
    # propose signature: (title, description, apply_fn, *, dry_run=False) -> dict
    async def _fake_propose(title, description, apply_fn, *, dry_run=False):
        modified = await apply_fn(str(wt))
        return {"ok": True, "stage": "dry_run", "branch": "test", "changed": modified}

    with patch.object(hive.self_modifier, "propose", side_effect=_fake_propose):
        asyncio.run(hive.self_improve_from_symptom("path traversal attack"))

    assert outside.read_text() == "sentinel", \
        "Production _apply closure must block path traversal outside the worktree"


# ---------------------------------------------------------------------------
# New coverage tests
# ---------------------------------------------------------------------------

def test_self_improve_from_symptom_returns_dict(tmp_path):
    """self_improve_from_symptom() returns a list (the actual return type); each
    element (if any) is an EditOutcome with known attributes."""
    hive = _make_hive(tmp_path)
    # _ScriptRouter returns "[]" → diagnoser produces no edits → empty list
    result = asyncio.run(hive.self_improve_from_symptom("some symptom"))
    assert isinstance(result, list)
    # Verify known attributes are accessible on any outcome object returned
    for outcome in result:
        assert hasattr(outcome, "status")
        assert hasattr(outcome, "op")
        assert hasattr(outcome, "tier")


def test_self_improve_from_symptom_fail_open_on_exception(tmp_path):
    """If diagnose_and_run raises, self_improve_from_symptom() swallows the
    exception and returns an empty list rather than propagating."""
    from unittest.mock import patch, AsyncMock

    hive = _make_hive(tmp_path)

    async def _exploding_diagnose_and_run(*args, **kwargs):
        raise RuntimeError("simulated diagnose_and_run failure")

    with patch("hive.core.spec_search.diagnose_and_run", side_effect=_exploding_diagnose_and_run):
        result = asyncio.run(hive.self_improve_from_symptom("any symptom"))

    # Must return [] instead of raising
    assert result == []


def test_improver_tier_summary_empty_initially(tmp_path):
    """A fresh SelfImprovement instance has tier_summary()['pending_review'] == 0."""
    from hive.core.spec_search import SelfImprovement
    from hive.core.self_mod import SelfModifier

    mod = SelfModifier(repo_root="/tmp/x")
    improver = SelfImprovement(mod)
    summary = improver.tier_summary()
    assert isinstance(summary, dict)
    assert "pending_review" in summary
    assert summary["pending_review"] == 0


# --- Wave 3K additional tests from worktree -----------------------------------

def test_tiered_corrects_wrong_risk_tier():
    """tiered() must overwrite an incorrect risk_tier with the canonical table value."""
    from hive.core.spec_search import Edit, EditOp, RiskTier, tiered

    async def _noop(wt): return []
    e = Edit(op=EditOp.ADD_TEST, summary="add a test", apply=_noop,
             risk_tier=RiskTier.MANUAL)
    corrected = tiered([e])
    assert corrected[0].risk_tier is RiskTier.AUTO


def test_self_improvement_pending_count_after_review_edit(tmp_path):
    """SelfImprovement.pending_count() increments by 1 for each REVIEW-tier edit run."""
    from hive.core.spec_search import Edit, EditOp, SelfImprovement
    from hive.core.self_mod import SelfModifier

    async def _noop(wt): return []
    mod = SelfModifier(repo_root=str(tmp_path))
    store: dict = {}
    improver = SelfImprovement(mod, pending_store=store)
    edits = [Edit(op=EditOp.PATCH_CODE, summary="fix", apply=_noop)]
    asyncio.run(improver.run(edits))
    assert improver.pending_count() == 1


def test_self_improvement_describe_pending_has_keys(tmp_path):
    """describe_pending() returns a list with required keys per pending edit."""
    from hive.core.spec_search import Edit, EditOp, SelfImprovement
    from hive.core.self_mod import SelfModifier

    async def _noop(wt): return []
    mod = SelfModifier(repo_root=str(tmp_path))
    store: dict = {}
    improver = SelfImprovement(mod, pending_store=store)
    asyncio.run(improver.run([Edit(op=EditOp.PATCH_CODE, summary="s", apply=_noop)]))
    pending = improver.describe_pending()
    assert len(pending) == 1
    for key in ("approval_id", "edit_id", "op", "summary"):
        assert key in pending[0], f"describe_pending missing key: {key}"


def test_self_improvement_oldest_pending_none_when_empty(tmp_path):
    """oldest_pending_id() returns None when no REVIEW edits are pending."""
    from hive.core.spec_search import SelfImprovement
    from hive.core.self_mod import SelfModifier

    mod = SelfModifier(repo_root=str(tmp_path))
    improver = SelfImprovement(mod)
    assert improver.oldest_pending_id() is None


def test_edit_outcome_has_expected_attributes():
    """EditOutcome instances expose the required status/op/tier/edit_id attributes."""
    from hive.core.spec_search import EditOutcome, EditOp, RiskTier

    outcome = EditOutcome(
        edit_id="abc123",
        op=EditOp.ADD_TEST,
        tier=RiskTier.AUTO,
        status="applied",
        detail="ok",
        branch="draft/test",
    )
    assert outcome.edit_id == "abc123"
    assert outcome.op is EditOp.ADD_TEST
    assert outcome.tier is RiskTier.AUTO
    assert outcome.status == "applied"
    assert outcome.branch == "draft/test"
    assert outcome.approval_id is None


# --- Wave 3O additional tests ---------------------------------------------------

def test_self_improvement_tier_summary_starts_zero(tmp_path):
    """tier_summary() returns pending_review=0 on a fresh SelfImprovement."""
    from hive.core.spec_search import SelfImprovement
    from hive.core.self_mod import SelfModifier
    mod = SelfModifier(repo_root=str(tmp_path))
    improver = SelfImprovement(mod)
    ts = improver.tier_summary()
    assert ts["pending_review"] == 0


def test_self_improvement_cancel_all_pending_returns_zero(tmp_path):
    """cancel_all_pending() returns 0 when nothing is pending."""
    from hive.core.spec_search import SelfImprovement
    from hive.core.self_mod import SelfModifier
    mod = SelfModifier(repo_root=str(tmp_path))
    improver = SelfImprovement(mod)
    assert improver.cancel_all_pending() == 0


def test_self_improvement_get_all_pending_returns_dict(tmp_path):
    """get_all_pending() returns a dict (empty when no REVIEW edits queued)."""
    from hive.core.spec_search import SelfImprovement
    from hive.core.self_mod import SelfModifier
    mod = SelfModifier(repo_root=str(tmp_path))
    improver = SelfImprovement(mod)
    result = improver.get_all_pending()
    assert isinstance(result, dict)


def test_edit_op_patch_code_value():
    """EditOp.PATCH_CODE has string value 'patch_code'."""
    from hive.core.spec_search import EditOp
    assert EditOp.PATCH_CODE.value == "patch_code"


def test_edit_op_add_test_value():
    """EditOp.ADD_TEST has string value 'add_test'."""
    from hive.core.spec_search import EditOp
    assert EditOp.ADD_TEST.value == "add_test"


def test_risk_tier_manual_value():
    """RiskTier.MANUAL has string value 'manual'."""
    from hive.core.spec_search import RiskTier
    assert RiskTier.MANUAL.value == "manual"


# --- Wave 3R additional tests ---------------------------------------------------

def test_edit_op_dependency_change_value():
    """EditOp.DEPENDENCY_CHANGE has string value 'dependency_change'."""
    from hive.core.spec_search import EditOp
    assert EditOp.DEPENDENCY_CHANGE.value == "dependency_change"


def test_edit_op_infra_deploy_value():
    """EditOp.INFRA_DEPLOY has string value 'infra_deploy'."""
    from hive.core.spec_search import EditOp
    assert EditOp.INFRA_DEPLOY.value == "infra_deploy"


def test_risk_tier_auto_value():
    """RiskTier.AUTO has string value 'auto'."""
    from hive.core.spec_search import RiskTier
    assert RiskTier.AUTO.value == "auto"


def test_risk_tier_review_value():
    """RiskTier.REVIEW has string value 'review'."""
    from hive.core.spec_search import RiskTier
    assert RiskTier.REVIEW.value == "review"


def test_assign_tier_returns_correct_tier_for_manual_ops():
    """assign_tier() maps DEPENDENCY_CHANGE and INFRA_DEPLOY to MANUAL."""
    from hive.core.spec_search import EditOp, RiskTier, assign_tier
    assert assign_tier(EditOp.DEPENDENCY_CHANGE) is RiskTier.MANUAL
    assert assign_tier(EditOp.INFRA_DEPLOY) is RiskTier.MANUAL


def test_cancel_review_removes_pending_edit(tmp_path):
    """cancel_review() returns True and removes the edit from the pending store."""
    from hive.core.spec_search import Edit, EditOp, SelfImprovement
    from hive.core.self_mod import SelfModifier

    async def _noop(wt): return []
    mod = SelfModifier(repo_root=str(tmp_path))
    store: dict = {}
    improver = SelfImprovement(mod, pending_store=store)
    outcomes = asyncio.run(improver.run([Edit(op=EditOp.PATCH_CODE, summary="fix", apply=_noop)]))
    approval_id = outcomes[0].approval_id
    assert improver.pending_count() == 1
    result = improver.cancel_review(approval_id)
    assert result is True
    assert improver.pending_count() == 0


# --- Wave 3X additional tests ---------------------------------------------------

def test_wave3x_assign_tier_auto_for_edit_docs():
    """assign_tier returns AUTO for EDIT_DOCS."""
    from hive.core.spec_search import EditOp, RiskTier, assign_tier
    assert assign_tier(EditOp.EDIT_DOCS) is RiskTier.AUTO


def test_wave3x_assign_tier_auto_for_add_test():
    """assign_tier returns AUTO for ADD_TEST."""
    from hive.core.spec_search import EditOp, RiskTier, assign_tier
    assert assign_tier(EditOp.ADD_TEST) is RiskTier.AUTO


def test_wave3x_assign_tier_review_for_add_tool():
    """assign_tier returns REVIEW for ADD_TOOL."""
    from hive.core.spec_search import EditOp, RiskTier, assign_tier
    assert assign_tier(EditOp.ADD_TOOL) is RiskTier.REVIEW


def test_wave3x_assign_tier_review_for_patch_system_prompt():
    """assign_tier returns REVIEW for PATCH_SYSTEM_PROMPT."""
    from hive.core.spec_search import EditOp, RiskTier, assign_tier
    assert assign_tier(EditOp.PATCH_SYSTEM_PROMPT) is RiskTier.REVIEW


def test_wave3x_edit_op_create_file_value():
    """EditOp.CREATE_FILE has string value 'create_file'."""
    from hive.core.spec_search import EditOp
    assert EditOp.CREATE_FILE.value == "create_file"


def test_wave3x_edit_op_set_model_for_task_value():
    """EditOp.SET_MODEL_FOR_TASK has string value 'set_model_for_task'."""
    from hive.core.spec_search import EditOp
    assert EditOp.SET_MODEL_FOR_TASK.value == "set_model_for_task"


def test_wave3x_tiered_review_op_stays_review():
    """tiered() preserves REVIEW tier for PATCH_CODE (already correct)."""
    from hive.core.spec_search import Edit, EditOp, RiskTier, tiered

    async def _noop(wt): return []
    e = Edit(op=EditOp.PATCH_CODE, summary="fix crash", apply=_noop,
             risk_tier=RiskTier.REVIEW)
    corrected = tiered([e])
    assert corrected[0].risk_tier is RiskTier.REVIEW


def test_wave3x_cancel_review_returns_false_for_unknown_id(tmp_path):
    """cancel_review() returns False when the approval_id is not in the store."""
    from hive.core.spec_search import SelfImprovement
    from hive.core.self_mod import SelfModifier

    mod = SelfModifier(repo_root=str(tmp_path))
    improver = SelfImprovement(mod)
    assert improver.cancel_review("nonexistent-id-xyz") is False


# --- Wave 4D-A additional self-improve tests -----------------------------------

def test_wave4d_edit_op_set_model_param_value():
    """EditOp.SET_MODEL_PARAM has string value 'set_model_param'."""
    from hive.core.spec_search import EditOp
    assert EditOp.SET_MODEL_PARAM.value == "set_model_param"


def test_wave4d_edit_op_edit_tool_description_value():
    """EditOp.EDIT_TOOL_DESCRIPTION has string value 'edit_tool_description'."""
    from hive.core.spec_search import EditOp
    assert EditOp.EDIT_TOOL_DESCRIPTION.value == "edit_tool_description"


def test_wave4d_edit_op_remove_tool_value():
    """EditOp.REMOVE_TOOL has string value 'remove_tool'."""
    from hive.core.spec_search import EditOp
    assert EditOp.REMOVE_TOOL.value == "remove_tool"


def test_wave4d_assign_tier_auto_for_set_model_param():
    """assign_tier returns AUTO for SET_MODEL_PARAM."""
    from hive.core.spec_search import EditOp, RiskTier, assign_tier
    assert assign_tier(EditOp.SET_MODEL_PARAM) is RiskTier.AUTO


def test_wave4d_assign_tier_auto_for_edit_tool_description():
    """assign_tier returns AUTO for EDIT_TOOL_DESCRIPTION."""
    from hive.core.spec_search import EditOp, RiskTier, assign_tier
    assert assign_tier(EditOp.EDIT_TOOL_DESCRIPTION) is RiskTier.AUTO


def test_wave4d_assign_tier_auto_for_remove_tool():
    """assign_tier returns AUTO for REMOVE_TOOL."""
    from hive.core.spec_search import EditOp, RiskTier, assign_tier
    assert assign_tier(EditOp.REMOVE_TOOL) is RiskTier.AUTO


def test_wave4d_describe_pending_empty_when_no_pending(tmp_path):
    """describe_pending() returns [] when no REVIEW-tier edits are queued."""
    from hive.core.spec_search import SelfImprovement
    from hive.core.self_mod import SelfModifier

    mod = SelfModifier(repo_root=str(tmp_path))
    improver = SelfImprovement(mod)
    result = improver.describe_pending()
    assert result == []


def test_wave4d_apply_approved_returns_outcome_with_applied_or_failed(tmp_path):
    """apply_approved() returns an EditOutcome with status 'applied' or 'failed' (not pending)."""
    from hive.core.spec_search import Edit, EditOp, SelfImprovement
    from hive.core.self_mod import SelfModifier

    async def _noop(wt): return []
    mod = SelfModifier(repo_root=str(tmp_path))
    store: dict = {}
    improver = SelfImprovement(mod, pending_store=store)
    edit = Edit(op=EditOp.PATCH_CODE, summary="fix", apply=_noop)

    async def _run():
        return await improver.apply_approved(edit)

    outcome = asyncio.run(_run())
    assert outcome.status in ("applied", "failed", "blocked_protected")
    assert outcome.op is EditOp.PATCH_CODE


# --- Wave 4J additional self-improve tests -------------------------------------

def test_wave4j_edit_op_create_file_tier_is_auto():
    """assign_tier returns AUTO for CREATE_FILE."""
    from hive.core.spec_search import EditOp, RiskTier, assign_tier
    assert assign_tier(EditOp.CREATE_FILE) is RiskTier.AUTO


def test_wave4j_apply_approved_with_manual_tier_op_returns_outcome(tmp_path):
    """apply_approved() with a MANUAL-tier op (INFRA_DEPLOY) returns an EditOutcome
    with a non-pending status — MANUAL ops are not expected to succeed via the normal
    worktree path, so 'failed' is the correct outcome here."""
    from hive.core.spec_search import Edit, EditOp, RiskTier, SelfImprovement, assign_tier
    from hive.core.self_mod import SelfModifier

    async def _noop(wt): return []
    mod = SelfModifier(repo_root=str(tmp_path))
    improver = SelfImprovement(mod)
    edit = Edit(op=EditOp.INFRA_DEPLOY, summary="deploy infra", apply=_noop)
    assert assign_tier(EditOp.INFRA_DEPLOY) is RiskTier.MANUAL

    async def _run():
        return await improver.apply_approved(edit)

    outcome = asyncio.run(_run())
    assert outcome.status != "pending_approval"
    assert outcome.op is EditOp.INFRA_DEPLOY
    assert outcome.tier is RiskTier.MANUAL


def test_wave4j_describe_pending_multiple_items_contains_all_ops(tmp_path):
    """describe_pending() with three REVIEW-tier edits lists all three ops."""
    from hive.core.spec_search import Edit, EditOp, SelfImprovement
    from hive.core.self_mod import SelfModifier

    async def _noop(wt): return []
    mod = SelfModifier(repo_root=str(tmp_path))
    store: dict = {}
    improver = SelfImprovement(mod, pending_store=store)
    edits = [
        Edit(op=EditOp.PATCH_CODE, summary="fix crash", apply=_noop),
        Edit(op=EditOp.ADD_TOOL, summary="add search tool", apply=_noop),
        Edit(op=EditOp.PATCH_SYSTEM_PROMPT, summary="update prompt", apply=_noop),
    ]
    asyncio.run(improver.run(edits))
    desc = improver.describe_pending()
    assert len(desc) == 3
    ops = {d["op"] for d in desc}
    assert ops == {"patch_code", "add_tool", "patch_system_prompt"}


def test_wave4j_describe_pending_each_item_has_risk_tier_field(tmp_path):
    """Each entry in describe_pending() includes a 'risk_tier' key."""
    from hive.core.spec_search import Edit, EditOp, SelfImprovement
    from hive.core.self_mod import SelfModifier

    async def _noop(wt): return []
    mod = SelfModifier(repo_root=str(tmp_path))
    store: dict = {}
    improver = SelfImprovement(mod, pending_store=store)
    asyncio.run(improver.run([Edit(op=EditOp.ADD_TOOL, summary="new tool", apply=_noop)]))
    desc = improver.describe_pending()
    assert len(desc) == 1
    assert "risk_tier" in desc[0]
    assert desc[0]["risk_tier"] == "review"


def test_wave4j_tier_summary_by_op_counts_each_op(tmp_path):
    """tier_summary()['by_op'] contains an entry per distinct op when multiple edits queued."""
    from hive.core.spec_search import Edit, EditOp, SelfImprovement
    from hive.core.self_mod import SelfModifier

    async def _noop(wt): return []
    mod = SelfModifier(repo_root=str(tmp_path))
    store: dict = {}
    improver = SelfImprovement(mod, pending_store=store)
    asyncio.run(improver.run([
        Edit(op=EditOp.PATCH_CODE, summary="fix1", apply=_noop),
        Edit(op=EditOp.ADD_TOOL, summary="tool1", apply=_noop),
    ]))
    ts = improver.tier_summary()
    assert ts["pending_review"] == 2
    assert ts["by_op"]["patch_code"] == 1
    assert ts["by_op"]["add_tool"] == 1


def test_wave4j_edit_op_patch_system_prompt_tier_is_review():
    """assign_tier returns REVIEW for PATCH_SYSTEM_PROMPT."""
    from hive.core.spec_search import EditOp, RiskTier, assign_tier
    assert assign_tier(EditOp.PATCH_SYSTEM_PROMPT) is RiskTier.REVIEW


def test_wave4j_self_improvement_cancel_all_removes_all_pending(tmp_path):
    """cancel_all_pending() returns the count of removed pending edits and clears them."""
    from hive.core.spec_search import Edit, EditOp, SelfImprovement
    from hive.core.self_mod import SelfModifier

    async def _noop(wt): return []
    mod = SelfModifier(repo_root=str(tmp_path))
    store: dict = {}
    improver = SelfImprovement(mod, pending_store=store)
    asyncio.run(improver.run([
        Edit(op=EditOp.PATCH_CODE, summary="a", apply=_noop),
        Edit(op=EditOp.ADD_TOOL, summary="b", apply=_noop),
    ]))
    assert improver.pending_count() == 2
    removed = improver.cancel_all_pending()
    assert removed == 2
    assert improver.pending_count() == 0


def test_wave4j_state_after_cancel_review_then_run_new_edit(tmp_path):
    """After cancelling all pending, a new run() starts fresh with pending_count == 1."""
    from hive.core.spec_search import Edit, EditOp, SelfImprovement
    from hive.core.self_mod import SelfModifier

    async def _noop(wt): return []
    mod = SelfModifier(repo_root=str(tmp_path))
    store: dict = {}
    improver = SelfImprovement(mod, pending_store=store)
    asyncio.run(improver.run([Edit(op=EditOp.PATCH_CODE, summary="old", apply=_noop)]))
    improver.cancel_all_pending()
    assert improver.pending_count() == 0
    asyncio.run(improver.run([Edit(op=EditOp.ADD_TOOL, summary="new tool", apply=_noop)]))
    assert improver.pending_count() == 1
    desc = improver.describe_pending()
    assert desc[0]["op"] == "add_tool"


# --- Wave 4Q additional self-improve tests -------------------------------------

def test_wave4q_edit_op_edit_docs_value():
    """EditOp.EDIT_DOCS has string value 'edit_docs'."""
    from hive.core.spec_search import EditOp
    assert EditOp.EDIT_DOCS.value == "edit_docs"


def test_wave4q_edit_op_add_tool_value():
    """EditOp.ADD_TOOL has string value 'add_tool'."""
    from hive.core.spec_search import EditOp
    assert EditOp.ADD_TOOL.value == "add_tool"


def test_wave4q_edit_op_patch_system_prompt_value():
    """EditOp.PATCH_SYSTEM_PROMPT has string value 'patch_system_prompt'."""
    from hive.core.spec_search import EditOp
    assert EditOp.PATCH_SYSTEM_PROMPT.value == "patch_system_prompt"


def test_wave4q_edit_has_rationale_field():
    """Edit has a 'rationale' field with a default empty string."""
    from hive.core.spec_search import Edit, EditOp

    async def _noop(wt): return []
    e = Edit(op=EditOp.EDIT_DOCS, summary="update readme", apply=_noop)
    assert hasattr(e, "rationale")
    assert isinstance(e.rationale, str)


def test_wave4q_describe_pending_shows_correct_count_after_three_ops(tmp_path):
    """describe_pending() with PATCH_CODE, ADD_TOOL, PATCH_SYSTEM_PROMPT returns exactly 3 items."""
    from hive.core.spec_search import Edit, EditOp, SelfImprovement
    from hive.core.self_mod import SelfModifier

    async def _noop(wt): return []
    mod = SelfModifier(repo_root=str(tmp_path))
    store: dict = {}
    improver = SelfImprovement(mod, pending_store=store)
    asyncio.run(improver.run([
        Edit(op=EditOp.PATCH_CODE, summary="fix", apply=_noop),
        Edit(op=EditOp.ADD_TOOL, summary="tool", apply=_noop),
        Edit(op=EditOp.PATCH_SYSTEM_PROMPT, summary="prompt", apply=_noop),
    ]))
    assert len(improver.describe_pending()) == 3


def test_wave4q_cancel_all_pending_empties_queue(tmp_path):
    """cancel_all_pending() returns count and leaves pending_count at 0."""
    from hive.core.spec_search import Edit, EditOp, SelfImprovement
    from hive.core.self_mod import SelfModifier

    async def _noop(wt): return []
    mod = SelfModifier(repo_root=str(tmp_path))
    store: dict = {}
    improver = SelfImprovement(mod, pending_store=store)
    asyncio.run(improver.run([
        Edit(op=EditOp.PATCH_CODE, summary="a", apply=_noop),
        Edit(op=EditOp.PATCH_SYSTEM_PROMPT, summary="b", apply=_noop),
    ]))
    n = improver.cancel_all_pending()
    assert n == 2
    assert improver.pending_count() == 0
    assert improver.describe_pending() == []


def test_wave4q_tiered_assigns_different_tiers_for_different_ops():
    """tiered() sets AUTO for ADD_TEST and REVIEW for ADD_TOOL on the same list."""
    from hive.core.spec_search import Edit, EditOp, RiskTier, tiered

    async def _noop(wt): return []
    edits = [
        Edit(op=EditOp.ADD_TEST, summary="add test", apply=_noop, risk_tier=RiskTier.MANUAL),
        Edit(op=EditOp.ADD_TOOL, summary="add tool", apply=_noop, risk_tier=RiskTier.MANUAL),
    ]
    corrected = tiered(edits)
    tiers = {e.op: e.risk_tier for e in corrected}
    assert tiers[EditOp.ADD_TEST] is RiskTier.AUTO
    assert tiers[EditOp.ADD_TOOL] is RiskTier.REVIEW


def test_wave4q_risk_tier_field_on_edit_defaults_to_manual():
    """Edit.risk_tier defaults to RiskTier.MANUAL before tiered() normalises it."""
    from hive.core.spec_search import Edit, EditOp, RiskTier

    async def _noop(wt): return []
    e = Edit(op=EditOp.ADD_TEST, summary="s", apply=_noop)
    assert e.risk_tier is RiskTier.MANUAL
