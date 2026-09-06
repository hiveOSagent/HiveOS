"""PILLAR 3 — learned_skills module + gateway endpoints.

Covers the full flow: detect repeated tool sequences from an audit log,
propose a SkillTemplate, persist it, approve+register it, and exercise the
generated body against a fake tool registry. Also covers the gateway routes
mounted in app.py.
"""
from __future__ import annotations

import asyncio
import pytest
from fastapi.testclient import TestClient

from hive.core.types import ToolResult
from hive.memory.skill_usage import SkillUsageStore
from hive.tools.learned_skills import (
    ALL_STATUSES,
    LearnedSkill,
    LearnedSkillStore,
    STATUS_APPROVED,
    STATUS_PROPOSED,
    STATUS_REGISTERED,
    STATUS_REJECTED,
    SkillTemplate,
    add_learned_skill,
    detect_patterns,
    propose_skill,
)


# ---- pattern detection -------------------------------------------------------

def test_detect_patterns_finds_repeated_sequences():
    seq = ["a", "b", "c", "a", "b", "c", "a", "b", "c", "a", "b", "c"]
    entries = [{"tool": t, "status": "ok"} for t in seq]
    pats = detect_patterns(entries, min_repeats=2, min_seq_len=3, max_seq_len=3)
    assert pats[0] == (("a", "b", "c"), 4)


def test_detect_patterns_ignores_failures():
    entries = (
        [{"tool": "a", "status": "ok"}, {"tool": "b", "status": "ok"}] * 5
        + [{"tool": "a", "status": "error"}, {"tool": "b", "status": "error"}] * 10
    )
    pats = detect_patterns(entries, min_repeats=2, min_seq_len=2, max_seq_len=2)
    # Only the 5 ok calls contribute; (a,b) appears 5 times.
    assert pats and pats[0] == (("a", "b"), 5)


def test_detect_patterns_drops_below_threshold():
    entries = [{"tool": "a", "status": "ok"}, {"tool": "b", "status": "ok"}]
    pats = detect_patterns(entries, min_repeats=3, min_seq_len=2, max_seq_len=2)
    assert pats == []


# ---- template proposal + persistence ----------------------------------------

def test_propose_skill_assigns_unique_name_and_code():
    t1 = propose_skill(("shell", "read_file"), seq_id="aaaaaa")
    t2 = propose_skill(("shell", "read_file"), seq_id="bbbbbb")
    assert t1.name != t2.name
    assert t1.status == STATUS_PROPOSED
    assert "call_tool" in t1.code and "shell" in t1.code


def test_propose_skill_rejects_empty_pattern():
    with pytest.raises(ValueError):
        propose_skill(())


# ---- dangerous-tool propagation (Batch G security fix) -----------------------

def test_propose_skill_inherits_dangerous_flag_from_constituent_tool():
    reg = _FakeRegistry()
    reg.add(_FakeTool("a"))
    reg.add(_FakeTool("shell_dangerous", dangerous=True))
    template = propose_skill(("a", "shell_dangerous"), registry=reg)
    assert template.dangerous is True


def test_propose_skill_stays_safe_when_no_constituent_is_dangerous():
    reg = _FakeRegistry()
    reg.add(_FakeTool("a"))
    reg.add(_FakeTool("b"))
    template = propose_skill(("a", "b"), registry=reg)
    assert template.dangerous is False


def test_propose_skill_treats_unknown_tool_as_dangerous():
    # Fail closed: a pattern referencing a tool absent from the registry must
    # not be assumed safe.
    reg = _FakeRegistry()
    reg.add(_FakeTool("a"))
    template = propose_skill(("a", "not_registered"), registry=reg)
    assert template.dangerous is True


def test_learned_skill_routes_constituent_calls_through_executor_gate(tmp_path):
    """The real gap this fix must close: `shell` is intentionally NOT
    spec.dangerous (routine commands stay fast — see builtins/__init__.py's
    module docstring); its danger is content-dependent, judged by
    gate.is_dangerous() inspecting the `cmd` string. A learned skill wrapping
    `shell` must not be able to launder a destructive command past that
    check just because the composite runs constituent calls internally."""
    from hive.tools.builtins import Shell
    from hive.tools.executor import DispatchStatus, ToolExecutor

    db = tmp_path / "ls.sqlite"
    store = LearnedSkillStore(db)
    registry: dict = {"shell": Shell()}
    executor = ToolExecutor(registry)

    template = propose_skill(("shell",), registry=registry)
    # Static inference correctly leaves this proposed as non-dangerous: shell
    # itself is not statically flagged, and that's by design.
    assert template.dangerous is False

    out = add_learned_skill(template, registry=registry, store=store,
                            auto_approve=True, executor=executor)
    assert out.status == STATUS_REGISTERED
    skill = registry[out.name]

    # A destructive command must be gated, not silently executed.
    result = asyncio.run(skill.execute(shell={"cmd": "rm -rf /some/path"}))
    assert result.success is False
    assert "requires approval" in result.content

    # A routine command still runs, unblocked.
    ok_result = asyncio.run(skill.execute(shell={"cmd": "echo hello"}))
    assert ok_result.success is True
    assert "hello" in ok_result.content
    store.close()


def test_add_learned_skill_reasserts_dangerous_even_if_template_forged(tmp_path):
    """A template that claims dangerous=False must not sail through registration
    if the live registry shows a constituent tool IS dangerous — this is the
    defense-in-depth re-check independent of whatever propose_skill saw."""
    db = tmp_path / "ls.sqlite"
    store = LearnedSkillStore(db)
    reg = _FakeRegistry()
    reg.add(_FakeTool("shell_dangerous", dangerous=True))
    # Built without a registry, so propose_skill had no chance to flag it.
    template = propose_skill(("shell_dangerous",))
    assert template.dangerous is False
    out = add_learned_skill(template, registry=reg, store=store, auto_approve=True)
    assert out.dangerous is True
    assert out.status == STATUS_REGISTERED
    # The registered LearnedSkill's own spec must carry the danger flag so the
    # ToolExecutor gates every future call to the composite.
    assert reg.snapshot()[out.name].spec.dangerous is True
    store.close()


def test_store_save_get_and_list_by_status(tmp_path):
    db = tmp_path / "ls.sqlite"
    store = LearnedSkillStore(db)
    t = propose_skill(("a", "b"))
    store.save(t)
    store.update_status(t.id, STATUS_APPROVED)
    fetched = store.get(t.id)
    assert fetched is not None and fetched.status == STATUS_APPROVED
    assert fetched.approved_ts is not None
    listed = store.list_by_status(STATUS_APPROVED)
    assert any(x.id == t.id for x in listed)
    store.close()


def test_store_observe_sequence_increments_and_filters(tmp_path):
    db = tmp_path / "ls.sqlite"
    store = LearnedSkillStore(db)
    assert store.observe_sequence(("a", "b")) == 1
    assert store.observe_sequence(("a", "b")) == 2
    assert store.observe_sequence(("a", "b")) == 3
    seqs = store.observed_sequences(min_count=2)
    assert seqs == [(("a", "b"), 3)]
    assert store.observed_sequences(min_count=10) == []
    store.close()


# ---- approve + register + execute -------------------------------------------

class _FakeTool:
    spec_name = "shell"
    def __init__(self, name: str, content: str = "ok", dangerous: bool = False) -> None:
        self._name = name
        self._content = content
        from hive.tools.base import ToolSpec
        self.spec = ToolSpec(name=name, description=f"fake {name}", category="test",
                             dangerous=dangerous)

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(tool_name=self._name, success=True, content=self._content)

    def available(self) -> bool:
        return True


class _FakeRegistry:
    def __init__(self) -> None:
        self._tools: dict = {}

    def add(self, tool):
        self._tools[tool.spec.name] = tool
        return tool

    def snapshot(self):
        return dict(self._tools)


def test_add_learned_skill_registers_and_runs(tmp_path):
    db = tmp_path / "ls.sqlite"
    store = LearnedSkillStore(db)
    usage = SkillUsageStore(":memory:")
    from hive.tools.executor import ToolExecutor
    reg = {"a": _FakeTool("a", "alpha"), "b": _FakeTool("b", "bravo"),
           "c": _FakeTool("c", "charlie")}
    executor = ToolExecutor(reg)
    template = propose_skill(("a", "b", "c"))
    store.save(template)
    out = add_learned_skill(template, registry=reg, skill_usage=usage,
                            store=store, auto_approve=True, executor=executor)
    assert out.status == STATUS_REGISTERED
    assert out.name in reg
    # SkillUsageStore should have a tracked row marked agent_created=True.
    row = usage.get(out.name)
    assert row is not None and row.agent_created is True
    # The generated body must call all three tools in order.
    async def _go():
        return await reg[out.name].execute(a={}, b={}, c={})
    result = asyncio.run(_go())
    assert result.success
    # Last tool's content is surfaced as the skill's content.
    assert result.content == "charlie"
    store.close()


def test_add_learned_skill_without_approval_stays_proposed(tmp_path):
    db = tmp_path / "ls.sqlite"
    store = LearnedSkillStore(db)
    reg = _FakeRegistry()
    reg.add(_FakeTool("a"))
    template = propose_skill(("a",))
    out = add_learned_skill(template, registry=reg, store=store, auto_approve=False)
    assert out.status == STATUS_PROPOSED
    assert reg.snapshot() == {"a": reg.snapshot()["a"]}
    store.close()


def test_approve_idempotent_on_existing_template(tmp_path):
    db = tmp_path / "ls.sqlite"
    store = LearnedSkillStore(db)
    reg = _FakeRegistry()
    reg.add(_FakeTool("a"))
    template = propose_skill(("a",))
    store.save(template)
    # Manually flip to approved so we can re-approve.
    store.update_status(template.id, STATUS_APPROVED)
    out = add_learned_skill(
        store.get(template.id), registry=reg, skill_usage=None,
        store=store, auto_approve=True,
    )
    assert out.status == STATUS_REGISTERED
    # Re-running is a no-op (registry already has the tool).
    out2 = add_learned_skill(out, registry=reg, skill_usage=None, store=store,
                             auto_approve=True)
    assert out2.status == STATUS_REGISTERED
    store.close()


def test_reject_marks_status_only(tmp_path):
    db = tmp_path / "ls.sqlite"
    store = LearnedSkillStore(db)
    t = propose_skill(("a",))
    store.save(t)
    assert store.update_status(t.id, STATUS_REJECTED)
    assert store.get(t.id).status == STATUS_REJECTED
    store.close()


def test_stats_reports_by_status(tmp_path):
    db = tmp_path / "ls.sqlite"
    store = LearnedSkillStore(db)
    for pattern, status in [(("a",), STATUS_PROPOSED),
                            (("a", "b"), STATUS_PROPOSED),
                            (("a", "b", "c"), STATUS_APPROVED)]:
        t = propose_skill(pattern)
        store.save(t)
        if status != STATUS_PROPOSED:
            store.update_status(t.id, status)
    s = store.stats()
    assert s["total"] == 3
    assert s["by_status"][STATUS_PROPOSED] == 2
    assert s["by_status"][STATUS_APPROVED] == 1
    store.close()


# ---- gateway endpoints ------------------------------------------------------

def _client(monkeypatch, tmp_path):
    """Build a TestClient with a minimal wired HiveOS (no real LLM)."""
    from hive.runtime import HiveOS
    monkeypatch.setenv("HIVE_SECRET", "test-secret")
    monkeypatch.setenv("HIVE_HOST", "127.0.0.1")
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HIVE_STATE_DB", str(tmp_path / "state.sqlite"))
    import hive.core.config as cfg_mod
    cfg_mod._CONFIG = None
    hive = HiveOS.build()
    from hive.gateway.app import create_app
    app = create_app(hive)
    return TestClient(app), hive


def test_gateway_list_learned_endpoint(monkeypatch, tmp_path):
    client, hive = _client(monkeypatch, tmp_path)
    # Seed one proposed template directly.
    t = propose_skill(("a", "b"))
    hive.learned_skills.save(t)
    r = client.get("/skills/learned", headers={"X-Hive-Token": "test-secret"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["templates"][0]["name"] == t.name


def test_gateway_propose_endpoint_persists(monkeypatch, tmp_path):
    client, hive = _client(monkeypatch, tmp_path)
    # Batch B: smoke runs against the live registry, so use a pattern of tools
    # that the HiveOS build() actually registers. ``hive_status`` + ``read_file``
    # + ``shell`` are all part of register_builtins.
    r = client.post(
        "/skills/learned/propose",
        headers={"X-Hive-Token": "test-secret"},
        json={"pattern": ["hive_status", "read_file", "shell"], "description": "demo"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == STATUS_PROPOSED
    assert body["smoke_result"] == "pass"
    assert body["pattern"] == ["hive_status", "read_file", "shell"]
    # And it shows up in list-by-status.
    r2 = client.get("/skills/learned?status=proposed",
                    headers={"X-Hive-Token": "test-secret"})
    assert r2.json()["count"] == 1


def test_gateway_propose_rejects_empty_pattern(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.post(
        "/skills/learned/propose",
        headers={"X-Hive-Token": "test-secret"},
        json={"pattern": []},
    )
    assert r.status_code == 422


def test_gateway_approve_endpoint_registers(monkeypatch, tmp_path):
    """Regression: hive.tools is a plain dict (no .snapshot()/.add()), so the
    approve endpoint must actually register the skill against a dict registry,
    not just report STATUS_APPROVED and silently skip registration."""
    client, hive = _client(monkeypatch, tmp_path)
    r = client.post(
        "/skills/learned/propose",
        headers={"X-Hive-Token": "test-secret"},
        json={"pattern": ["hive_status", "read_file"]},
    )
    template_id = r.json()["id"]
    r2 = client.post(
        f"/skills/learned/{template_id}/approve",
        headers={"X-Hive-Token": "test-secret"},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == STATUS_REGISTERED
    assert body["name"] in hive.tools


def test_gateway_skill_state_archive_deregisters_learned_skill(monkeypatch, tmp_path):
    """Batch H: POST /skills/{name}/state to 'archived' must remove a registered
    learned skill from hive.tools (routed through the Curator, not a bare
    skill_usage.set_state write) — and restoring it via 'active' brings it back."""
    client, hive = _client(monkeypatch, tmp_path)
    r = client.post(
        "/skills/learned/propose",
        headers={"X-Hive-Token": "test-secret"},
        json={"pattern": ["hive_status", "read_file"]},
    )
    template_id = r.json()["id"]
    r2 = client.post(
        f"/skills/learned/{template_id}/approve",
        headers={"X-Hive-Token": "test-secret"},
    )
    name = r2.json()["name"]
    assert name in hive.tools

    r3 = client.post(
        f"/skills/{name}/state",
        headers={"X-Hive-Token": "test-secret"},
        json={"state": "archived"},
    )
    assert r3.status_code == 200, r3.text
    assert name not in hive.tools
    # Batch K: GET /skills/learned?status=registered must not keep listing an
    # archived (deregistered) skill as registered — learned_skills.status has
    # to track the live state, not just skill_usage.state.
    listed = client.get("/skills/learned?status=registered",
                        headers={"X-Hive-Token": "test-secret"}).json()
    assert name not in {t["name"] for t in listed["templates"]}

    r4 = client.post(
        f"/skills/{name}/state",
        headers={"X-Hive-Token": "test-secret"},
        json={"state": "active"},
    )
    assert r4.status_code == 200, r4.text
    assert name in hive.tools
    listed = client.get("/skills/learned?status=registered",
                        headers={"X-Hive-Token": "test-secret"}).json()
    assert name in {t["name"] for t in listed["templates"]}


def test_gateway_reject_endpoint(monkeypatch, tmp_path):
    client, hive = _client(monkeypatch, tmp_path)
    t = propose_skill(("x", "y"))
    hive.learned_skills.save(t)
    r = client.post(
        f"/skills/learned/{t.id}/reject",
        headers={"X-Hive-Token": "test-secret"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == STATUS_REJECTED


def test_gateway_detect_endpoint(monkeypatch, tmp_path):
    client, hive = _client(monkeypatch, tmp_path)
    # Seed the audit log with a repeating 3-tool sequence.
    seq = ["shell", "read_file", "shell"] * 6
    for t in seq:
        hive.audit_log.record({"tool": t, "status": "ok", "args": {}})
    r = client.post(
        "/skills/learned/detect",
        headers={"X-Hive-Token": "test-secret"},
        json={"min_repeats": 3, "min_seq_len": 3, "max_seq_len": 3, "limit": 5},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scanned_entries"] >= 6
    seqs = [tuple(p["sequence"]) for p in body["patterns"]]
    assert ("shell", "read_file", "shell") in seqs
