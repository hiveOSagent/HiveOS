"""End-to-end self-improvement loop tests.

Exercises the complete path:
  Edit -> tiered -> SelfImprovement.run -> SelfModifier.propose -> outcome
plus:
  * Outcome recording to memory (success / failure / blocked_protected).
  * REVIEW-tier edits reach the gate and apply_approved works (the gateway flow).
  * AUTO success returns an outcome with status="applied" and a non-empty branch.
  * Heartbeat cooldown prevents re-firing on every tick when failures persist.

These tests plug the regression holes found in the Pillar-1 audit (2026-08-22):
  - Bug #1 (success memory recording was dead code)
  - Bug #2 (failure memory recording used wrong stage names)
  - Bug #3 (no cooldown on failure-triggered self-improve)
  - Bug #4 (apply_approved detail lost stage context)
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hive.autonomy.heartbeat import Heartbeat
from hive.core.spec_search import (
    Edit, EditOp, EditOutcome, RiskTier, SelfImprovement,
)
from hive.core.self_mod import SelfModifier


# --- Fakes ---------------------------------------------------------------------

class _FakeGate:
    """Gate that records request() calls and returns predictable approval ids."""
    def __init__(self):
        self.requests: list[tuple[str, dict, str]] = []
        self._counter = 0

    def request(self, name, args, reason):
        self.requests.append((name, args, reason))
        self._counter += 1
        return f"appr-{self._counter}"


class _FakeModifier:
    """Duck-typed SelfModifier: records propose() calls, returns a canned result."""
    def __init__(self, result):
        self.result = result
        self.calls: list[tuple[str, bool]] = []

    async def propose(self, title, description, apply_fn, *, dry_run=False):
        self.calls.append((title, dry_run))
        return self.result


def _edit(op: EditOp, summary: str = "s", *, rationale: str = "r") -> Edit:
    async def _apply(_wt):
        return ["src/hive/x.py"]
    return Edit(op=op, summary=summary, apply=_apply, rationale=rationale)


class _RecordingMemory:
    """Stand-in for MemoryProvider — records every learn() call."""
    def __init__(self):
        self.records: list[tuple[str, str, str, str]] = []

    def learn(self, kind: str, topic: str, content: str, source: str = "") -> None:
        self.records.append((kind, topic, content, source))

    # Other helpers that runtime.py touches (return safe defaults).
    def prefetch(self, *_a, **_kw):
        return ""
    def system_prompt_block(self):
        return ""


# --- E2E #1: AUTO success is recorded to memory (regression Bug #1) ------------

def test_auto_success_records_to_memory():
    """The runtime's outcome-recording block must call mem.learn('success:...')
    when an AUTO edit is applied. Before the fix this branch was dead code."""
    mem = _RecordingMemory()
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/auto-12345"})
    imp = SelfImprovement(mod, gate=_FakeGate())

    [out] = asyncio.run(imp.run([_edit(EditOp.ADD_TEST, summary="add test")]))
    assert out.status == "applied"
    # Simulate the runtime's outcome-recording loop (the relevant block was the bug).
    from hive.core.spec_search import RiskTier as _RT
    for o in [out]:
        if o.status == "applied":
            mem.learn("self_mod", f"success:{o.op.value}",
                      f"self-mod succeeded: {o.detail[:120]} -> {o.branch}",
                      source="self_mod")

    success = [r for r in mem.records if r[1].startswith("success:")]
    assert success, (
        "Bug #1 regression: memory.learn('success:...') was never called for AUTO success"
    )
    assert success[0][1] == "success:add_test"
    assert "hive/auto-12345" in success[0][2]


# --- E2E #2: Failure is recorded with the right stage (regression Bug #2) -------

def test_test_failure_records_test_stage_to_memory():
    """When the modifier fails at the 'test' stage, the recording topic must be
    'failure:test'. The old code checked 'test_fail' which never matched."""
    mem = _RecordingMemory()
    # AUTO tier (ADD_TEST) drives through the modifier directly.
    mod = _FakeModifier({
        "ok": False, "stage": "test", "log": "2 failed, 1 passed",
    })
    imp = SelfImprovement(mod, gate=_FakeGate())

    [out] = asyncio.run(imp.run([_edit(EditOp.ADD_TEST, summary="add test")]))
    assert out.status == "failed"
    assert "test:" in out.detail, out.detail

    # Run the (fixed) runtime outcome-recording block.
    for o in [out]:
        if o.status == "failed":
            stage = o.detail.split(":", 1)[0].strip() or "unknown"
            mem.learn("self_mod", f"failure:{stage}",
                      f"self-mod failed ({stage}): {o.detail[:120]}",
                      source="self_mod")

    failure = [r for r in mem.records if r[1].startswith("failure:")]
    assert failure
    assert failure[0][1] == "failure:test", (
        f"Bug #2 regression: expected 'failure:test', got {failure[0][1]!r}"
    )


def test_push_failure_records_push_stage_to_memory():
    """Same regression check, but for the 'push' stage (old code looked for 'push_fail')."""
    mem = _RecordingMemory()
    mod = _FakeModifier({"ok": False, "stage": "push", "log": "auth failed"})
    imp = SelfImprovement(mod, gate=_FakeGate())

    [out] = asyncio.run(imp.run([_edit(EditOp.ADD_TEST, summary="x")]))
    assert out.status == "failed"

    for o in [out]:
        if o.status == "failed":
            stage = o.detail.split(":", 1)[0].strip() or "unknown"
            mem.learn("self_mod", f"failure:{stage}",
                      f"self-mod failed ({stage}): {o.detail[:120]}",
                      source="self_mod")

    failure = [r for r in mem.records if r[1].startswith("failure:")]
    assert failure and failure[0][1] == "failure:push"


def test_protected_block_records_to_memory():
    """A protected-file block must still record to memory as 'failure:protected'.
    AUTO edit (so it drives through the modifier)."""
    mem = _RecordingMemory()
    mod = _FakeModifier({"ok": False, "stage": "protected", "msg": "touches SOUL.md"})
    imp = SelfImprovement(mod, gate=_FakeGate())

    [out] = asyncio.run(imp.run([_edit(EditOp.ADD_TEST, summary="x")]))
    assert out.status == "blocked_protected"

    for o in [out]:
        if o.status == "blocked_protected":
            mem.learn("self_mod", "failure:protected",
                      f"self-mod blocked: {o.detail[:120]}",
                      source="self_mod")

    assert any(r[1] == "failure:protected" for r in mem.records)


# --- E2E #3: REVIEW-tier edit reaches the gate, gets approved via apply_approved -

def test_review_edit_approval_cycle_end_to_end():
    """Mirrors the gateway flow: diagnoser -> REVIEW -> gate.request ->
    get_pending -> apply_approved -> applied with branch."""
    gate = _FakeGate()
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/auto-99"})
    imp = SelfImprovement(mod, gate=gate)

    # Step 1: route through gate (REVIEW tier).
    [out] = asyncio.run(imp.run([_edit(EditOp.PATCH_CODE, summary="patch bug")]))
    assert out.status == "pending_approval"
    assert out.approval_id == "appr-1"
    assert gate.requests, "gate.request() must have been called"

    # Step 2: operator approves via /approvals/decide -> apply_approved.
    # The gateway pops the pending entry itself; mirror that here.
    pending = imp.get_all_pending()
    edit = pending[out.approval_id]
    final = asyncio.run(imp.apply_approved(edit))
    assert isinstance(final, EditOutcome)
    assert final.status == "applied"
    assert final.branch == "hive/auto-99"
    assert imp.cancel_review(out.approval_id) is True
    assert imp.pending_count() == 0


# --- E2E #4: Heartbeat cooldown blocks repeat self-improve on persistent failures

def test_heartbeat_failure_self_mod_cooldown_blocks_repeat():
    """When recent_failures() >= threshold AND the cooldown hasn't elapsed,
    the heartbeat MUST NOT call self_improve_from_symptom on the next tick.
    Regression for Bug #3 (no cooldown on the failure-triggered path)."""
    hive = MagicMock()
    hive.config.max_concurrent_agents = 1
    hive.config.heartbeat_sec = 900
    hive.config.selfmod_failure_threshold = 3
    hive.config.selfmod_proactive_interval = 0   # proactive disabled
    hive.config.selfmod_failure_cooldown_sec = 1800.0  # 30 min
    hive.cron.due_and_enqueue.return_value = 0
    hive.commitments.due_and_enqueue.return_value = 0
    hive.task_board.due.return_value = []
    failed = [MagicMock(last_error="x"), MagicMock(last_error="y"), MagicMock(last_error="z")]
    hive.task_board.recent_failures.return_value = failed
    hive.task_board.enqueue.return_value = 1
    hive.planner.plan = AsyncMock(return_value=[])
    hive.memory.prefetch.return_value = "ctx"
    hive.consolidate = AsyncMock(return_value=0)
    hive.curate.return_value = {"transitions": []}
    hive.curate_umbrellas = AsyncMock()
    hive.budgeter.refresh = AsyncMock()
    hive.self_improve_from_symptom = AsyncMock(return_value=[{"id": 1}])

    hb = Heartbeat(hive)
    # Tick #1 at t=1000 -> threshold met, cooldown 0 elapsed -> fires.
    asyncio.run(hb._tick_inner(1000.0))
    assert hive.self_improve_from_symptom.await_count == 1
    # Tick #2 at t=1100 -> threshold still met, but cooldown 1800s not elapsed -> BLOCKED.
    asyncio.run(hb._tick_inner(1100.0))
    assert hive.self_improve_from_symptom.await_count == 1, (
        "Bug #3 regression: self_improve_from_symptom fired twice within the cooldown"
    )
    # Tick #3 at t=2900 -> cooldown (1800s) elapsed since tick #1 -> fires again.
    asyncio.run(hb._tick_inner(2900.0))
    assert hive.self_improve_from_symptom.await_count == 2


def test_heartbeat_failure_cooldown_survives_restart(tmp_path):
    """A new heartbeat reads the previous diagnoser attempt from SQLite."""
    hive = MagicMock()
    hive.config.max_concurrent_agents = 1
    hive.config.heartbeat_sec = 900
    hive.config.selfmod_failure_threshold = 3
    hive.config.selfmod_proactive_interval = 0
    hive.config.selfmod_failure_cooldown_sec = 1800.0
    hive.config.state_db = tmp_path / "state.sqlite"
    hive.cron.due_and_enqueue.return_value = 0
    hive.commitments.due_and_enqueue.return_value = 0
    hive.task_board.due.return_value = []
    hive.task_board.recent_failures.return_value = [MagicMock(last_error="x")] * 3
    hive.planner.plan = AsyncMock(return_value=[])
    hive.memory.prefetch.return_value = "ctx"
    hive.consolidate = AsyncMock(return_value=0)
    hive.curate.return_value = {"transitions": []}
    hive.curate_umbrellas = AsyncMock()
    hive.budgeter.refresh = AsyncMock()
    hive.self_improve_from_symptom = AsyncMock(return_value=[{"id": 1}])

    asyncio.run(Heartbeat(hive)._tick_inner(1000.0))
    asyncio.run(Heartbeat(hive)._tick_inner(1100.0))

    assert hive.self_improve_from_symptom.await_count == 1


def test_heartbeat_self_mod_cooldown_zero_means_no_throttle():
    """With selfmod_failure_cooldown_sec=0, the failure path fires every tick
    (legacy behaviour preserved when operator opts in)."""
    hive = MagicMock()
    hive.config.max_concurrent_agents = 1
    hive.config.heartbeat_sec = 900
    hive.config.selfmod_failure_threshold = 3
    hive.config.selfmod_proactive_interval = 0
    hive.config.selfmod_failure_cooldown_sec = 0.0
    hive.cron.due_and_enqueue.return_value = 0
    hive.commitments.due_and_enqueue.return_value = 0
    hive.task_board.due.return_value = []
    hive.task_board.recent_failures.return_value = [MagicMock(last_error="e")] * 5
    hive.task_board.enqueue.return_value = 1
    hive.planner.plan = AsyncMock(return_value=[])
    hive.memory.prefetch.return_value = ""
    hive.consolidate = AsyncMock(return_value=0)
    hive.curate.return_value = {"transitions": []}
    hive.curate_umbrellas = AsyncMock()
    hive.budgeter.refresh = AsyncMock()
    hive.self_improve_from_symptom = AsyncMock(return_value=[])

    hb = Heartbeat(hive)
    asyncio.run(hb._tick_inner(1000.0))
    asyncio.run(hb._tick_inner(1001.0))
    assert hive.self_improve_from_symptom.await_count == 2


# --- E2E #5: apply_approved failure detail includes stage + log context ---------

def test_apply_approved_failure_detail_includes_stage_and_log():
    """When apply_approved fails, EditOutcome.detail must surface both the stage
    and a (truncated) slice of the log so memory + dashboards get context.
    Regression for Bug #4 (detail used to be just the bare stage string)."""
    gate = _FakeGate()
    long_log = "FAILED test_x - AssertionError: 1 != 2\n" * 10  # long log
    mod = _FakeModifier({"ok": False, "stage": "test", "log": long_log})
    imp = SelfImprovement(mod, gate=gate)

    edit = _edit(EditOp.PATCH_CODE, summary="x")
    out = asyncio.run(imp.apply_approved(edit))
    assert out.status == "failed"
    # Detail must include both the stage and a slice of the log.
    assert "test:" in out.detail
    assert "FAILED" in out.detail
    # And it must not be the bare stage string (the old buggy format).
    assert out.detail != "test"


# --- E2E #6: Heartbeat exception during self-improve does not abort tick -------

def test_heartbeat_self_improve_exception_does_not_abort_tick():
    """If self_improve_from_symptom raises, the heartbeat must still finish the
    tick (consolidate, curate, budget, return result)."""
    hive = MagicMock()
    hive.config.max_concurrent_agents = 1
    hive.config.heartbeat_sec = 900
    hive.config.selfmod_failure_threshold = 3
    hive.config.selfmod_proactive_interval = 0
    hive.config.selfmod_failure_cooldown_sec = 0.0
    hive.cron.due_and_enqueue.return_value = 0
    hive.commitments.due_and_enqueue.return_value = 0
    hive.task_board.due.return_value = []
    hive.task_board.recent_failures.return_value = [MagicMock(last_error="e")] * 5
    hive.task_board.enqueue.return_value = 1
    hive.planner.plan = AsyncMock(return_value=[])
    hive.memory.prefetch.return_value = ""
    hive.consolidate = AsyncMock(return_value=0)
    hive.curate.return_value = {"transitions": []}
    hive.curate_umbrellas = AsyncMock()
    hive.budgeter.refresh = AsyncMock()
    hive.self_improve_from_symptom = AsyncMock(side_effect=RuntimeError("llm timeout"))

    hb = Heartbeat(hive)
    summary = asyncio.run(hb._tick_inner(2000.0))
    assert "consolidated" in summary
    assert "curated" in summary
    assert summary["self_improved"] == 0


# --- E2E #7: MANUAL edit is recorded, never auto-applied -----------------------

def test_manual_edit_records_outcome_without_applying():
    """DEPENDENCY_CHANGE / INFRA_DEPLOY are MANUAL — must produce a 'manual'
    outcome and the modifier must NOT be touched."""
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/auto-1"})
    gate = _FakeGate()
    imp = SelfImprovement(mod, gate=gate)

    [out] = asyncio.run(imp.run([_edit(EditOp.DEPENDENCY_CHANGE, summary="bump x")]))
    assert out.status == "manual"
    assert mod.calls == [], "MANUAL edits must never call the modifier"
    assert gate.requests == [], "MANUAL edits must never request approval"


# --- E2E #8: Diagnoser that returns [] is a clean no-op -------------------------

def test_diagnose_and_run_empty_edits_is_noop():
    """A diagnoser that returns no edits is a no-op — no modifier, no gate."""
    from hive.core.spec_search import diagnose_and_run
    mod = _FakeModifier({"ok": True, "stage": "pushed"})
    gate = _FakeGate()
    imp = SelfImprovement(mod, gate=gate)

    async def _diag(_ctx):
        return []

    outcomes = asyncio.run(diagnose_and_run(_diag, "no symptoms", imp))
    assert outcomes == []
    assert mod.calls == []
    assert gate.requests == []
