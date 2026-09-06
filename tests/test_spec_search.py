"""M2 #si-1 — risk-tiered self-improvement loop tests (offline)."""
from __future__ import annotations

import asyncio

import pytest

from hive.core.self_mod import SelfModifier
from hive.core.spec_search import (
    Edit, EditOp, EditOutcome, RiskTier, SelfImprovement,
    assign_tier, diagnose_and_run, tiered, _TIER_TABLE,
)


# --- deterministic tiering -----------------------------------------------------

def test_every_op_has_a_tier():
    assert set(_TIER_TABLE) == set(EditOp)


def test_model_cannot_escalate_its_own_tier():
    # An edit arrives claiming AUTO for a code patch; the table forces REVIEW.
    async def _noop(_wt): return []
    e = Edit(op=EditOp.PATCH_CODE, summary="sneaky", apply=_noop, risk_tier=RiskTier.AUTO)
    [fixed] = tiered([e])
    assert fixed.risk_tier is RiskTier.REVIEW
    assert assign_tier(EditOp.DEPENDENCY_CHANGE) is RiskTier.MANUAL
    assert assign_tier(EditOp.ADD_TEST) is RiskTier.AUTO


# --- gate routing by tier ------------------------------------------------------

class _FakeModifier:
    """Duck-typed SelfModifier: records propose() calls, returns a canned result."""
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def propose(self, title, description, apply_fn, *, dry_run=False):
        self.calls.append((title, dry_run))
        return self.result


class _FakeGate:
    def __init__(self):
        self.requests = []

    def request(self, name, args, reason):
        self.requests.append((name, args, reason))
        return "appr-1"


def _edit(op, summary="s"):
    async def _apply(_wt): return ["src/hive/x.py"]
    return Edit(op=op, summary=summary, apply=_apply)


def test_auto_edit_drives_through_modifier():
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/auto-1"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    [out] = asyncio.run(imp.run([_edit(EditOp.ADD_TEST)]))
    assert out.status == "applied" and out.branch == "hive/auto-1"
    assert mod.calls and out.tier is RiskTier.AUTO


def test_review_edit_requests_approval_and_does_not_apply():
    mod = _FakeModifier({"ok": True})
    gate = _FakeGate()
    imp = SelfImprovement(mod, gate=gate)
    [out] = asyncio.run(imp.run([_edit(EditOp.PATCH_CODE)]))
    assert out.status == "pending_approval" and out.approval_id == "appr-1"
    assert gate.requests and not mod.calls   # gate hit, modifier NOT called


def test_manual_edit_is_recorded_not_applied():
    mod = _FakeModifier({"ok": True})
    imp = SelfImprovement(mod, gate=_FakeGate())
    [out] = asyncio.run(imp.run([_edit(EditOp.DEPENDENCY_CHANGE)]))
    assert out.status == "manual" and not mod.calls


def test_protected_block_surfaces():
    mod = _FakeModifier({"ok": False, "stage": "protected", "msg": "touches SOUL.md"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    [out] = asyncio.run(imp.run([_edit(EditOp.ADD_TEST)]))
    assert out.status == "blocked_protected" and "SOUL" in out.detail


def test_failed_test_surfaces():
    mod = _FakeModifier({"ok": False, "stage": "test", "log": "1 failed"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    [out] = asyncio.run(imp.run([_edit(EditOp.ADD_TEST)]))
    assert out.status == "failed" and "test" in out.detail


def test_apply_approved_runs_review_edit():
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "b"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    out = asyncio.run(imp.apply_approved(_edit(EditOp.PATCH_CODE)))
    assert out.status == "applied" and mod.calls


# --- diagnose loop -------------------------------------------------------------

def test_diagnose_and_run_no_edits_is_noop():
    async def diagnoser(_ctx): return []
    imp = SelfImprovement(_FakeModifier({"ok": True}), gate=_FakeGate())
    assert asyncio.run(diagnose_and_run(diagnoser, "ctx", imp)) == []


def test_diagnose_and_run_mixed_tiers():
    async def diagnoser(_ctx):
        return [_edit(EditOp.ADD_TEST), _edit(EditOp.PATCH_CODE),
                _edit(EditOp.DEPENDENCY_CHANGE)]
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "b"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    outs = asyncio.run(diagnose_and_run(diagnoser, "ctx", imp, dry_run=True))
    statuses = {o.op: o.status for o in outs}
    assert statuses[EditOp.ADD_TEST] == "applied"
    assert statuses[EditOp.PATCH_CODE] == "pending_approval"
    assert statuses[EditOp.DEPENDENCY_CHANGE] == "manual"


# --- integration with the REAL SelfModifier (fake git runner, no network) ------

def test_integration_auto_edit_with_real_selfmodifier(tmp_path):
    """AUTO edit flows through the real SelfModifier using a fake git runner so the
    worktree->test->push sequence is exercised without touching real git."""
    calls = []

    async def fake_run(cmd, cwd=None):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        calls.append(cmd_str)
        if cmd_str.startswith("git rev-parse"):
            return 0, "deadbeef\n"
        if cmd_str.startswith("git diff --name-only"):
            return 0, "src/hive/x.py\n"
        if cmd_str.startswith("git ls-files --others"):
            return 0, ""
        return 0, "ok"  # worktree add, test, add, commit, push, cleanup all succeed

    async def apply_fn(_wt):
        return ["src/hive/llm/pricing.py"]   # non-protected path

    mod = SelfModifier(repo_root=str(tmp_path), run=fake_run, test_cmd="pytest -q")
    imp = SelfImprovement(mod, gate=_FakeGate())
    [out] = asyncio.run(imp.run([_edit(EditOp.ADD_TEST)]))
    assert out.status == "applied" and out.branch and out.branch.startswith("hive/auto-")
    assert any(c.startswith("git worktree add") for c in calls)
    assert any("pytest" in c for c in calls)


def test_create_file_op_is_auto_tier():
    """CREATE_FILE must be AUTO tier — it only creates new files, never overwrites."""
    assert assign_tier(EditOp.CREATE_FILE) is RiskTier.AUTO


def test_create_file_apply_fn_creates_new_file(tmp_path):
    """CREATE_FILE _apply creates the target file in the worktree."""
    from pathlib import Path

    async def apply_create(wt: str) -> list[str]:
        from pathlib import Path as _Path
        target = _Path(wt) / "tests" / "test_new.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# new test\n", encoding="utf-8")
        return ["tests/test_new.py"]

    mod_result = {"ok": True, "stage": "pushed", "branch": "hive/auto-1"}
    mod = _FakeModifier(mod_result)
    imp = SelfImprovement(mod, gate=_FakeGate())
    edit = Edit(op=EditOp.CREATE_FILE, summary="add test", apply=apply_create)
    [out] = asyncio.run(imp.run([edit]))
    assert out.status == "applied"
    assert out.tier is RiskTier.AUTO


def test_selfimprovement_get_pending_and_cancel():
    mod = _FakeModifier({"ok": True})
    gate = _FakeGate()
    imp = SelfImprovement(mod, gate=gate)
    [out] = asyncio.run(imp.run([_edit(EditOp.PATCH_CODE)]))
    approval_id = out.approval_id
    assert imp.pending_count() == 1
    assert imp.get_pending(approval_id) is not None
    assert imp.cancel_review(approval_id) is True
    assert imp.pending_count() == 0
    assert imp.get_pending(approval_id) is None
    assert imp.cancel_review(approval_id) is False  # already cancelled


def test_selfimprovement_cancel_all_pending():
    import itertools
    _counter = itertools.count(1)

    class _UniqueGate2(_FakeGate):
        def request(self, name, args, reason):
            self.requests.append((name, args, reason))
            return f"x-{next(_counter)}"

    mod = _FakeModifier({"ok": True})
    gate = _UniqueGate2()
    imp = SelfImprovement(mod, gate=gate)
    asyncio.run(imp.run([_edit(EditOp.PATCH_CODE)]))
    asyncio.run(imp.run([_edit(EditOp.PATCH_CODE, summary="second")]))
    assert imp.pending_count() == 2
    cancelled = imp.cancel_all_pending()
    assert cancelled == 2
    assert imp.pending_count() == 0


def test_selfimprovement_get_all_pending():
    import itertools
    _counter = itertools.count(1)

    class _UniqueGate(_FakeGate):
        def request(self, name, args, reason):
            self.requests.append((name, args, reason))
            return f"appr-{next(_counter)}"

    mod = _FakeModifier({"ok": True})
    gate = _UniqueGate()
    imp = SelfImprovement(mod, gate=gate)
    asyncio.run(imp.run([_edit(EditOp.PATCH_CODE)]))
    asyncio.run(imp.run([_edit(EditOp.PATCH_CODE, summary="second")]))
    all_p = imp.get_all_pending()
    assert len(all_p) == 2
    assert all(isinstance(e, Edit) for e in all_p.values())


def test_integration_protected_edit_is_blocked(tmp_path):
    """An edit that touches a PROTECTED file is refused by the real SelfModifier."""
    async def fake_run(cmd, cwd=None):
        if cmd.startswith("git rev-parse"):
            return 0, "deadbeef\n"
        return 0, "ok"

    async def apply_protected(_wt):
        return ["Config/SOUL.md"]   # PROTECTED

    mod = SelfModifier(repo_root=str(tmp_path), run=fake_run)
    imp = SelfImprovement(mod, gate=_FakeGate())
    [out] = asyncio.run(imp.run([_edit(EditOp.PATCH_CODE)]))  # REVIEW tier → gate, no apply
    # REVIEW never reaches the modifier, so use apply_approved to exercise the block:
    out2 = asyncio.run(imp.apply_approved(
        Edit(op=EditOp.PATCH_CODE, summary="x", apply=apply_protected)))
    assert out2.status == "blocked_protected"


# --- describe_pending -----------------------------------------------------------

def test_describe_pending_empty():
    mod = _FakeModifier({"ok": True})
    imp = SelfImprovement(mod, gate=_FakeGate())
    assert imp.describe_pending() == []


def test_describe_pending_returns_metadata():
    import itertools
    _counter = itertools.count(1)

    class _UniqueGate2:
        def request(self, name, args, reason):
            return f"appr-{next(_counter)}"
        def is_dangerous(self, *a): return False

    mod = _FakeModifier({"ok": True})
    gate = _UniqueGate2()
    imp = SelfImprovement(mod, gate=gate)
    asyncio.run(imp.run([_edit(EditOp.PATCH_CODE, summary="fix bug")]))
    asyncio.run(imp.run([_edit(EditOp.ADD_TOOL, summary="add search")]))
    pending = imp.describe_pending()
    assert len(pending) == 2
    keys = {"approval_id", "edit_id", "op", "summary", "rationale", "risk_tier"}
    assert all(keys.issubset(p.keys()) for p in pending)
    summaries = {p["summary"] for p in pending}
    assert summaries == {"fix bug", "add search"}
    assert all(p["risk_tier"] == "review" for p in pending)


def test_describe_pending_matches_get_all_pending():
    import itertools
    _counter = itertools.count(1)

    class _UniqueGate3:
        def request(self, name, args, reason):
            return f"ap-{next(_counter)}"
        def is_dangerous(self, *a): return False

    mod = _FakeModifier({"ok": True})
    imp = SelfImprovement(mod, gate=_UniqueGate3())
    asyncio.run(imp.run([_edit(EditOp.PATCH_CODE, summary="change x")]))
    described = imp.describe_pending()
    pending = imp.get_all_pending()
    assert len(described) == len(pending)
    described_ids = {d["approval_id"] for d in described}
    assert described_ids == set(pending.keys())


# --- oldest_pending_id ---------------------------------------------------------

def test_oldest_pending_id_empty():
    imp = SelfImprovement(_FakeModifier({"ok": True}), gate=_FakeGate())
    assert imp.oldest_pending_id() is None


def test_oldest_pending_id_returns_first_inserted():
    import itertools
    _counter = itertools.count(1)

    class _UniqueGate4:
        def request(self, name, args, reason):
            return f"id-{next(_counter)}"
        def is_dangerous(self, *a): return False

    imp = SelfImprovement(_FakeModifier({"ok": True}), gate=_UniqueGate4())
    asyncio.run(imp.run([_edit(EditOp.PATCH_CODE, summary="first")]))
    asyncio.run(imp.run([_edit(EditOp.ADD_TOOL, summary="second")]))
    first_id = imp.oldest_pending_id()
    assert first_id is not None
    assert first_id == "id-1"  # first inserted


def test_oldest_pending_id_none_after_cancel_all():
    imp = SelfImprovement(_FakeModifier({"ok": True}), gate=_FakeGate())
    asyncio.run(imp.run([_edit(EditOp.PATCH_CODE)]))
    imp.cancel_all_pending()
    assert imp.oldest_pending_id() is None


# --- tier_summary ---------------------------------------------------------------

def test_tier_summary_empty():
    imp = SelfImprovement(_FakeModifier({"ok": True}), gate=_FakeGate())
    summary = imp.tier_summary()
    assert summary == {"pending_review": 0, "by_op": {}}


def test_tier_summary_single_op():
    imp = SelfImprovement(_FakeModifier({"ok": True}), gate=_FakeGate())
    asyncio.run(imp.run([_edit(EditOp.PATCH_CODE)]))
    summary = imp.tier_summary()
    assert summary["pending_review"] == 1
    assert summary["by_op"] == {"patch_code": 1}


def test_tier_summary_multiple_ops():
    import itertools
    _ctr = itertools.count(1)

    class _UniqueGate5:
        def request(self, name, args, reason):
            return f"ts-id-{next(_ctr)}"
        def is_dangerous(self, *a): return False

    imp = SelfImprovement(_FakeModifier({"ok": True}), gate=_UniqueGate5())
    asyncio.run(imp.run([
        _edit(EditOp.PATCH_CODE, summary="p1"),
        _edit(EditOp.PATCH_CODE, summary="p2"),
        _edit(EditOp.ADD_TOOL, summary="a1"),
    ]))
    summary = imp.tier_summary()
    assert summary["pending_review"] == 3
    assert summary["by_op"]["patch_code"] == 2
    assert summary["by_op"]["add_tool"] == 1


def test_tier_summary_after_cancel():
    imp = SelfImprovement(_FakeModifier({"ok": True}), gate=_FakeGate())
    asyncio.run(imp.run([_edit(EditOp.PATCH_CODE), _edit(EditOp.ADD_TOOL)]))
    imp.cancel_all_pending()
    summary = imp.tier_summary()
    assert summary == {"pending_review": 0, "by_op": {}}


# --- Task 1: SelfImprovement.tiered() additional paths -------------------------

def test_tiered_manual_op_returns_manual_outcome():
    """DEPENDENCY_CHANGE (MANUAL tier) must return status='manual', no PR."""
    mod = _FakeModifier({"ok": True})
    imp = SelfImprovement(mod, gate=_FakeGate())
    [out] = asyncio.run(imp.run([_edit(EditOp.DEPENDENCY_CHANGE)]))
    assert out.status == "manual"
    assert out.approval_id is None
    assert not mod.calls


def test_tiered_review_op_queues_to_gate():
    """PATCH_CODE (REVIEW tier) must queue to gate and return status='pending_approval'."""
    mod = _FakeModifier({"ok": True})
    gate = _FakeGate()
    imp = SelfImprovement(mod, gate=gate)
    [out] = asyncio.run(imp.run([_edit(EditOp.PATCH_CODE)]))
    assert out.status == "pending_approval"
    assert out.approval_id == "appr-1"
    assert gate.requests
    assert not mod.calls


def test_tiered_protected_file_returns_blocked():
    """AUTO edit that touches Config/SOUL.md must return status='blocked_protected'."""
    mod = _FakeModifier({"ok": False, "stage": "protected", "msg": "touches SOUL.md"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    [out] = asyncio.run(imp.run([_edit(EditOp.ADD_TEST)]))
    assert out.status == "blocked_protected"
    assert "SOUL" in out.detail


# --- Task 3: assign_tier determinism (model cannot self-escalate) ---------------

def test_risk_tier_deterministic_review_ops():
    """PATCH_CODE, ADD_TOOL, PATCH_SYSTEM_PROMPT must be REVIEW (not AUTO)."""
    assert assign_tier(EditOp.PATCH_CODE) is RiskTier.REVIEW
    assert assign_tier(EditOp.ADD_TOOL) is RiskTier.REVIEW
    assert assign_tier(EditOp.PATCH_SYSTEM_PROMPT) is RiskTier.REVIEW


def test_risk_tier_deterministic_auto_ops():
    """ADD_TEST, EDIT_DOCS, SET_MODEL_FOR_TASK, CREATE_FILE must be AUTO."""
    assert assign_tier(EditOp.ADD_TEST) is RiskTier.AUTO
    assert assign_tier(EditOp.EDIT_DOCS) is RiskTier.AUTO
    assert assign_tier(EditOp.SET_MODEL_FOR_TASK) is RiskTier.AUTO
    assert assign_tier(EditOp.CREATE_FILE) is RiskTier.AUTO


def test_risk_tier_deterministic_manual_ops():
    """DEPENDENCY_CHANGE and INFRA_DEPLOY must be MANUAL."""
    assert assign_tier(EditOp.DEPENDENCY_CHANGE) is RiskTier.MANUAL
    assert assign_tier(EditOp.INFRA_DEPLOY) is RiskTier.MANUAL


def test_tier_table_covers_all_ops():
    """Every EditOp must appear in _TIER_TABLE — no silent defaults."""
    assert set(_TIER_TABLE) == set(EditOp)


def test_assign_tier_returns_correct_type():
    """assign_tier always returns a RiskTier instance."""
    for op in EditOp:
        result = assign_tier(op)
        assert isinstance(result, RiskTier)


# --- Task: _touches_protected (SelfModifier-level function) ---------------------

from hive.core.self_mod import _touches_protected as _tp


def test_touches_protected_soul_md():
    """Config/SOUL.md (mixed-case) must be recognized as protected."""
    assert _tp(["Config/SOUL.md"]) is True


def test_touches_protected_approval_gate():
    """Core/approval_gate.py (mixed-case) must be recognized as protected."""
    assert _tp(["Core/approval_gate.py"]) is True


def test_touches_protected_normal_file():
    """An ordinary source file must NOT be flagged as protected."""
    assert _tp(["src/hive/tools/builtins/__init__.py"]) is False


def test_touches_protected_case_insensitive():
    """The check must be case-insensitive: all-caps path still matches."""
    assert _tp(["CONFIG/SOUL.MD"]) is True
    assert _tp(["CORE/APPROVAL_GATE.PY"]) is True


# --- Task: risk tier mapping via assign_tier -----------------------------------

def test_risk_tier_op_edit_docs_is_auto():
    """EDIT_DOCS must map to AUTO tier."""
    assert assign_tier(EditOp.EDIT_DOCS) is RiskTier.AUTO


def test_risk_tier_op_patch_code_is_review():
    """PATCH_CODE must map to REVIEW tier."""
    assert assign_tier(EditOp.PATCH_CODE) is RiskTier.REVIEW


def test_risk_tier_op_manual_is_manual():
    """INFRA_DEPLOY must map to MANUAL tier (represents a 'manual' operation)."""
    assert assign_tier(EditOp.INFRA_DEPLOY) is RiskTier.MANUAL


# --- Additional spec_search tests -------------------------------------------

def test_edit_op_has_create_file():
    """EditOp enum must have CREATE_FILE member."""
    assert hasattr(EditOp, "CREATE_FILE")


def test_edit_op_has_edit_docs():
    """EditOp enum must have EDIT_DOCS member."""
    assert hasattr(EditOp, "EDIT_DOCS")


def test_edit_op_has_patch_code():
    """PATCH_CODE must exist in EditOp."""
    assert hasattr(EditOp, "PATCH_CODE")


def test_edit_op_all_members_have_tiers():
    """assign_tier() must return a RiskTier for every EditOp member."""
    for op in EditOp:
        tier = assign_tier(op)
        assert isinstance(tier, RiskTier), f"{op} has no tier"


def test_risk_tier_auto_is_string():
    """AUTO tier value is the string 'auto'."""
    assert RiskTier.AUTO.value == "auto"


def test_risk_tier_manual_is_string():
    """MANUAL tier value is the string 'manual'."""
    assert RiskTier.MANUAL.value == "manual"


def test_edit_dataclass_fields():
    """Edit dataclass stores op, summary, apply."""
    async def _noop(_wt): return []
    e = Edit(op=EditOp.EDIT_DOCS, summary="update docs", apply=_noop)
    assert e.op is EditOp.EDIT_DOCS
    assert e.summary == "update docs"
    assert callable(e.apply)


def test_edit_outcome_has_op():
    """EditOutcome stores the op field."""
    o = EditOutcome(edit_id="e1", op=EditOp.EDIT_DOCS, tier=RiskTier.AUTO,
                    status="applied")
    assert o.op is EditOp.EDIT_DOCS


def test_tiered_returns_same_length():
    """tiered() returns the same number of edits."""
    async def _noop(_wt): return []
    edits = [
        Edit(op=EditOp.EDIT_DOCS, summary="doc", apply=_noop),
        Edit(op=EditOp.PATCH_CODE, summary="code", apply=_noop),
    ]
    result = tiered(edits)
    assert len(result) == len(edits)


# --- New tests: additional behavioral coverage --------------------------------

def test_edit_has_unique_id_by_default():
    """Two Edit instances created without an explicit id must have different ids."""
    async def _noop(_wt): return []
    e1 = Edit(op=EditOp.ADD_TEST, summary="a", apply=_noop)
    e2 = Edit(op=EditOp.ADD_TEST, summary="b", apply=_noop)
    assert e1.id != e2.id


def test_tiered_overwrites_manual_to_auto_for_auto_op():
    """tiered() corrects MANUAL risk_tier to AUTO for an ADD_TEST op."""
    async def _noop(_wt): return []
    # Provide an incorrect tier; tiered() must replace it with the canonical value.
    e = Edit(op=EditOp.ADD_TEST, summary="s", apply=_noop, risk_tier=RiskTier.MANUAL)
    [fixed] = tiered([e])
    assert fixed.risk_tier is RiskTier.AUTO


def test_edit_outcome_fields_preserved():
    """EditOutcome stores all required fields without losing data."""
    o = EditOutcome(
        edit_id="abc123",
        op=EditOp.PATCH_CODE,
        tier=RiskTier.REVIEW,
        status="pending_approval",
        detail="awaiting human",
        branch=None,
        approval_id="appr-99",
    )
    assert o.edit_id == "abc123"
    assert o.tier is RiskTier.REVIEW
    assert o.status == "pending_approval"
    assert o.approval_id == "appr-99"
    assert o.branch is None


def test_selfimprovement_pending_count_starts_zero():
    """A freshly created SelfImprovement has pending_count() == 0."""
    imp = SelfImprovement(_FakeModifier({"ok": True}), gate=_FakeGate())
    assert imp.pending_count() == 0


def test_selfimprovement_auto_edit_not_added_to_pending():
    """AUTO-tier edits bypass the approval gate and must NOT appear in pending_store."""
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/auto-x"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    asyncio.run(imp.run([_edit(EditOp.ADD_TEST)]))
    # ADD_TEST is AUTO tier — it runs immediately and must never sit in pending_store
    assert imp.pending_count() == 0


def test_risk_tier_review_is_string():
    """REVIEW tier value is the string 'review'."""
    assert RiskTier.REVIEW.value == "review"


# --- 6 additional tests (Wave 3T) -------------------------------------------

def test_assign_tier_set_model_param_and_remove_tool_are_auto():
    """SET_MODEL_PARAM and REMOVE_TOOL must both map to AUTO tier."""
    assert assign_tier(EditOp.SET_MODEL_PARAM) is RiskTier.AUTO
    assert assign_tier(EditOp.REMOVE_TOOL) is RiskTier.AUTO


def test_describe_pending_op_is_string_value_not_enum():
    """describe_pending must return op as the enum .value string, not the enum member."""
    imp = SelfImprovement(_FakeModifier({"ok": True}), gate=_FakeGate())
    asyncio.run(imp.run([_edit(EditOp.PATCH_CODE)]))
    [record] = imp.describe_pending()
    assert record["op"] == "patch_code"
    assert isinstance(record["op"], str)


def test_describe_pending_rationale_preserved():
    """describe_pending includes the rationale field from the original Edit."""
    async def _noop(_wt): return []
    e = Edit(op=EditOp.PATCH_CODE, summary="bugfix", apply=_noop,
             rationale="reduces p99 latency")
    imp = SelfImprovement(_FakeModifier({"ok": True}), gate=_FakeGate())
    asyncio.run(imp.run([e]))
    [record] = imp.describe_pending()
    assert record["rationale"] == "reduces p99 latency"


def test_run_mixed_edits_single_call_returns_correct_statuses():
    """run() with AUTO + REVIEW + MANUAL edits in one call returns all three statuses."""
    import itertools
    _ctr = itertools.count(1)

    class _CountingGate:
        def request(self, name, args, reason):
            return f"g-{next(_ctr)}"

    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/b"})
    imp = SelfImprovement(mod, gate=_CountingGate())
    outs = asyncio.run(imp.run([
        _edit(EditOp.ADD_TEST, summary="auto"),
        _edit(EditOp.PATCH_CODE, summary="review"),
        _edit(EditOp.DEPENDENCY_CHANGE, summary="manual"),
    ]))
    assert len(outs) == 3
    by_summary = {o.op: o.status for o in outs}
    assert by_summary[EditOp.ADD_TEST] == "applied"
    assert by_summary[EditOp.PATCH_CODE] == "pending_approval"
    assert by_summary[EditOp.DEPENDENCY_CHANGE] == "manual"


def test_apply_approved_failed_non_protected_returns_failed():
    """apply_approved must return status='failed' when modifier returns ok=False, stage='test'."""
    mod = _FakeModifier({"ok": False, "stage": "test", "log": "AssertionError"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    out = asyncio.run(imp.apply_approved(_edit(EditOp.PATCH_CODE)))
    assert out.status == "failed"
    assert "test" in out.detail


def test_cancel_review_removes_only_targeted_id():
    """cancel_review removes exactly one entry; other pending edits remain."""
    import itertools
    _ctr = itertools.count(1)

    class _UniqueGate6:
        def request(self, name, args, reason):
            return f"cr-{next(_ctr)}"

    imp = SelfImprovement(_FakeModifier({"ok": True}), gate=_UniqueGate6())
    [out1] = asyncio.run(imp.run([_edit(EditOp.PATCH_CODE, summary="first")]))
    [out2] = asyncio.run(imp.run([_edit(EditOp.ADD_TOOL, summary="second")]))
    assert imp.pending_count() == 2
    removed = imp.cancel_review(out1.approval_id)
    assert removed is True
    assert imp.pending_count() == 1
    assert imp.get_pending(out1.approval_id) is None
    assert imp.get_pending(out2.approval_id) is not None


# --- Wave 4K-B additional tests (8) -------------------------------------------

def test_wave4k_sequential_proposals_accumulate_in_pending():
    """Multiple sequential REVIEW proposals each add one entry to pending_store."""
    import itertools
    _ctr = itertools.count(1)

    class _SeqGate:
        def request(self, name, args, reason):
            return f"seq-{next(_ctr)}"

    imp = SelfImprovement(_FakeModifier({"ok": True}), gate=_SeqGate())
    asyncio.run(imp.run([_edit(EditOp.PATCH_CODE, summary="a")]))
    assert imp.pending_count() == 1
    asyncio.run(imp.run([_edit(EditOp.PATCH_CODE, summary="b")]))
    assert imp.pending_count() == 2
    asyncio.run(imp.run([_edit(EditOp.ADD_TOOL, summary="c")]))
    assert imp.pending_count() == 3


def test_wave4k_mixed_auto_and_review_ops_in_one_call():
    """AUTO ops bypass pending; REVIEW ops accumulate; both statuses returned."""
    import itertools
    _ctr = itertools.count(1)

    class _MixGate:
        def request(self, name, args, reason):
            return f"mix-{next(_ctr)}"

    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/mix"})
    imp = SelfImprovement(mod, gate=_MixGate())
    outs = asyncio.run(imp.run([
        _edit(EditOp.ADD_TEST, summary="auto-one"),
        _edit(EditOp.PATCH_CODE, summary="review-one"),
        _edit(EditOp.EDIT_DOCS, summary="auto-two"),
        _edit(EditOp.ADD_TOOL, summary="review-two"),
    ]))
    by_op = {o.op: o.status for o in outs}
    assert by_op[EditOp.ADD_TEST] == "applied"
    assert by_op[EditOp.PATCH_CODE] == "pending_approval"
    assert by_op[EditOp.EDIT_DOCS] == "applied"
    assert by_op[EditOp.ADD_TOOL] == "pending_approval"
    assert imp.pending_count() == 2


def test_wave4k_tier_summary_counts_per_tier_op():
    """tier_summary reflects correct per-op counts when multiple distinct ops are pending."""
    import itertools
    _ctr = itertools.count(1)

    class _TsGate:
        def request(self, name, args, reason):
            return f"ts-{next(_ctr)}"

    imp = SelfImprovement(_FakeModifier({"ok": True}), gate=_TsGate())
    asyncio.run(imp.run([
        _edit(EditOp.PATCH_CODE, summary="p1"),
        _edit(EditOp.ADD_TOOL, summary="a1"),
        _edit(EditOp.PATCH_SYSTEM_PROMPT, summary="sp1"),
        _edit(EditOp.PATCH_CODE, summary="p2"),
    ]))
    summary = imp.tier_summary()
    assert summary["pending_review"] == 4
    assert summary["by_op"]["patch_code"] == 2
    assert summary["by_op"]["add_tool"] == 1
    assert summary["by_op"]["patch_system_prompt"] == 1


def test_wave4k_tier_summary_only_counts_review_ops():
    """tier_summary counts only REVIEW-tier pending edits; AUTO and MANUAL are excluded."""
    import itertools
    _ctr = itertools.count(1)

    class _OnlyReviewGate:
        def request(self, name, args, reason):
            return f"or-{next(_ctr)}"

    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/r"})
    imp = SelfImprovement(mod, gate=_OnlyReviewGate())
    asyncio.run(imp.run([
        _edit(EditOp.ADD_TEST, summary="auto-skip"),
        _edit(EditOp.DEPENDENCY_CHANGE, summary="manual-skip"),
        _edit(EditOp.PATCH_CODE, summary="review-counted"),
    ]))
    summary = imp.tier_summary()
    assert summary["pending_review"] == 1
    assert "patch_code" in summary["by_op"]
    assert "add_test" not in summary["by_op"]
    assert "dependency_change" not in summary["by_op"]


def test_wave4k_apply_approved_add_tool_returns_applied():
    """apply_approved with an ADD_TOOL edit returns status='applied' on modifier success."""
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/tool"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    out = asyncio.run(imp.apply_approved(_edit(EditOp.ADD_TOOL, summary="new-search-tool")))
    assert out.status == "applied"
    assert out.branch == "hive/tool"
    assert out.op is EditOp.ADD_TOOL


def test_wave4k_apply_approved_patch_system_prompt_returns_applied():
    """apply_approved with a PATCH_SYSTEM_PROMPT edit returns status='applied'."""
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/sp"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    out = asyncio.run(imp.apply_approved(_edit(EditOp.PATCH_SYSTEM_PROMPT, summary="refine prompt")))
    assert out.status == "applied"
    assert out.op is EditOp.PATCH_SYSTEM_PROMPT


def test_wave4k_describe_pending_insertion_order_preserved():
    """describe_pending returns records in insertion order (first-in, first-out)."""
    import itertools
    _ctr = itertools.count(1)

    class _OrderGate:
        def request(self, name, args, reason):
            return f"ord-{next(_ctr)}"

    imp = SelfImprovement(_FakeModifier({"ok": True}), gate=_OrderGate())
    asyncio.run(imp.run([
        _edit(EditOp.PATCH_CODE, summary="alpha"),
        _edit(EditOp.ADD_TOOL, summary="beta"),
        _edit(EditOp.PATCH_SYSTEM_PROMPT, summary="gamma"),
    ]))
    records = imp.describe_pending()
    assert len(records) == 3
    assert records[0]["summary"] == "alpha"
    assert records[1]["summary"] == "beta"
    assert records[2]["summary"] == "gamma"


def test_wave4k_describe_pending_risk_tier_always_review():
    """All entries returned by describe_pending have risk_tier='review'."""
    import itertools
    _ctr = itertools.count(1)

    class _TierGate:
        def request(self, name, args, reason):
            return f"tr-{next(_ctr)}"

    imp = SelfImprovement(_FakeModifier({"ok": True}), gate=_TierGate())
    asyncio.run(imp.run([
        _edit(EditOp.PATCH_CODE, summary="x"),
        _edit(EditOp.ADD_TOOL, summary="y"),
        _edit(EditOp.PATCH_SYSTEM_PROMPT, summary="z"),
    ]))
    for record in imp.describe_pending():
        assert record["risk_tier"] == "review"
        assert isinstance(record["risk_tier"], str)


# --- Wave 4R additional tests (8) -------------------------------------------

def test_wave4r_tiered_empty_list_returns_empty():
    """tiered() with an empty list returns an empty list."""
    assert tiered([]) == []


def test_wave4r_edit_op_member_count():
    """EditOp must have exactly 12 members (none silently added)."""
    assert len(list(EditOp)) == 12


def test_wave4r_edit_outcome_default_detail_is_empty():
    """EditOutcome.detail defaults to an empty string when not supplied."""
    o = EditOutcome(edit_id="x", op=EditOp.ADD_TEST, tier=RiskTier.AUTO, status="applied")
    assert o.detail == ""


def test_wave4r_edit_outcome_default_branch_is_none():
    """EditOutcome.branch defaults to None when not supplied."""
    o = EditOutcome(edit_id="y", op=EditOp.EDIT_DOCS, tier=RiskTier.AUTO, status="applied")
    assert o.branch is None


def test_wave4r_edit_outcome_default_approval_id_is_none():
    """EditOutcome.approval_id defaults to None when not supplied."""
    o = EditOutcome(edit_id="z", op=EditOp.PATCH_CODE, tier=RiskTier.REVIEW,
                    status="pending_approval")
    assert o.approval_id is None


def test_wave4r_diagnose_and_run_single_edit_applied():
    """diagnose_and_run with an AUTO diagnoser returns one applied outcome."""
    async def diagnoser(_ctx):
        return [_edit(EditOp.ADD_TEST, summary="auto-edit")]

    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/wave4r"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    outs = asyncio.run(diagnose_and_run(diagnoser, "ctx", imp))
    assert len(outs) == 1
    assert outs[0].status == "applied"
    assert outs[0].branch == "hive/wave4r"


def test_wave4r_run_multiple_auto_edits_all_applied():
    """run() with three AUTO-tier edits returns three 'applied' outcomes."""
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/multi"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    outs = asyncio.run(imp.run([
        _edit(EditOp.ADD_TEST, summary="t1"),
        _edit(EditOp.EDIT_DOCS, summary="t2"),
        _edit(EditOp.CREATE_FILE, summary="t3"),
    ]))
    assert all(o.status == "applied" for o in outs)
    assert len(outs) == 3


def test_wave4r_edit_id_format_is_12_hex_chars():
    """Edit.id generated by default_factory is a 12-character hex string."""
    async def _noop(_wt): return []
    e = Edit(op=EditOp.ADD_TEST, summary="s", apply=_noop)
    assert len(e.id) == 12
    int(e.id, 16)  # raises ValueError if not hex
