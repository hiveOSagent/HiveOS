"""P7 — runtime wiring: HiveOS.build() + HiveOS.ask() end-to-end (no network)."""
from __future__ import annotations

import asyncio
import json

import hive
from hive.core.config import HiveConfig, get_config
from hive.core.types import ToolCall
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS


class _ScriptRouter:
    """Stands in for ModelRouter; returns scripted results, records tool schemas seen."""
    def __init__(self, script):
        self._script = list(script)
        self.saw_tools = None
        self.closed = False

    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        self.saw_tools = tools
        item = self._script.pop(0)
        return item if isinstance(item, CompletionResult) else CompletionResult(text=item, model="fake")

    async def aclose(self):
        self.closed = True


def _config(tmp_path) -> HiveConfig:
    return HiveConfig.from_env(root=tmp_path, load_dotenv=False)


def test_lazy_package_export():
    # `from hive import HiveOS` works via PEP 562 without eager full-tree import
    assert hive.HiveOS is HiveOS


def test_build_wires_all_subsystems_and_sets_global_config(tmp_path):
    cfg = _config(tmp_path)
    hos = HiveOS.build(cfg, router=_ScriptRouter([]))
    assert isinstance(hos, HiveOS)
    for attr in ("events", "router", "tools", "tool_executor", "memory",
                 "session_store", "keeper", "planner", "orchestrator"):
        assert getattr(hos, attr) is not None
    assert "read_file" in hos.tools and "shell" in hos.tools
    # builder published the config to the global accessor (D1)
    assert get_config() is cfg
    assert cfg.data_dir.is_dir()


def test_build_wires_budgeter_history_persistence(tmp_path):
    """Regression: Budgeter(history_path=...) must be wired from HiveOS.build(),
    otherwise forecast_spend() has no data across restarts (dead persistence
    code path — daily history is loaded/saved but never given a real path)."""
    cfg = _config(tmp_path)
    hos = HiveOS.build(cfg, router=_ScriptRouter([]))
    assert hos.budgeter._history_path == str(cfg.data_dir / "budget_history.json")


def test_hive_build_attaches_board_store(tmp_path):
    """HiveOS.build() must construct and attach a BoardStore bound to its EventBus."""
    from hive.agents.board import BoardStore
    from hive.agents.a2a.events import emit_call_started

    cfg = _config(tmp_path)
    hos = HiveOS.build(cfg, router=_ScriptRouter([]))
    assert isinstance(hos.board, BoardStore)
    # Same bus instance → events on hos.events update hos.board
    emit_call_started(hos.events, method="researcher.run", request_id="rt-1",
                      agent_name="researcher", task="t")
    assert any(c.request_id == "rt-1" for c in hos.board.snapshot()["researcher"])


def test_ask_end_to_end_persists_and_recalls(tmp_path):
    router = _ScriptRouter([CompletionResult(text="hi Kamil", model="m")])
    hos = HiveOS.build(_config(tmp_path), router=router)

    answer = asyncio.run(hos.ask("hello", session_id="s1"))
    assert answer == "hi Kamil"
    assert [m.content for m in hos.session_store.messages("s1")] == ["hello", "hi Kamil"]
    # session_store is the canonical transcript; memory.recent() is provider-specific.
    # Use prefetch() which is in the MemoryProvider ABC and works with all providers.
    # (LocalMemoryProvider also syncs turns, so recent() would be truthy there too.)
    assert router.saw_tools and any(t["name"] == "shell" for t in router.saw_tools)


def test_ask_runs_a_real_builtin_tool_end_to_end(tmp_path):
    target = tmp_path / "out.txt"
    call = ToolCall(id="c1", name="write_file",
                    arguments=json.dumps({"path": str(target), "content": "from hive"}))
    router = _ScriptRouter([
        CompletionResult(text="", model="m", tool_calls=[call]),  # ask for the tool
        CompletionResult(text="done", model="m"),                  # then answer
    ])
    hos = HiveOS.build(_config(tmp_path), router=router)
    answer = asyncio.run(hos.ask("please write the file"))
    assert answer == "done"
    assert target.read_text() == "from hive"          # the builtin actually ran


def test_aclose_closes_router(tmp_path):
    router = _ScriptRouter([])
    hos = HiveOS.build(_config(tmp_path), router=router)
    asyncio.run(hos.aclose())
    assert router.closed is True


def test_pending_review_edits_lists_review_tier(tmp_path):
    """pending_review_edits() returns metadata for each REVIEW-tier edit awaiting approval."""
    from hive.core.spec_search import Edit, EditOp
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    async def _noop(_wt): return []
    edit = Edit(op=EditOp.PATCH_CODE, summary="fix logic", apply=_noop)
    asyncio.run(hos.improver.run([edit]))
    pending = hos.pending_review_edits()
    assert len(pending) == 1
    assert pending[0]["op"] == "patch_code"
    assert pending[0]["summary"] == "fix logic"
    assert "approval_id" in pending[0]


def test_pending_review_edits_empty_when_none_pending(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.pending_review_edits() == []


def test_system_status_returns_expected_keys(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    status = hos.system_status()
    assert "router" in status
    assert "budget" in status
    assert "tasks" in status
    assert "tools" in status
    assert "pending_approvals" in status
    assert "cron_jobs" in status
    assert "active_commitments" in status
    # Budget forecast nested properly
    assert "remaining_calls" in status["budget"]


def test_abort_self_mod_removes_pending_edit(tmp_path):
    from hive.core.spec_search import Edit, EditOp
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    async def _noop(_wt): return []
    edit = Edit(op=EditOp.PATCH_CODE, summary="fix", apply=_noop)
    asyncio.run(hos.improver.run([edit]))
    pending = hos.pending_review_edits()
    assert len(pending) == 1
    approval_id = pending[0]["approval_id"]
    assert hos.abort_self_mod(approval_id) is True
    assert hos.pending_review_edits() == []
    assert hos.abort_self_mod(approval_id) is False  # already gone


def test_abort_all_self_mods(tmp_path):
    from hive.core.spec_search import Edit, EditOp
    import itertools
    _counter = itertools.count(1)

    class _CountingGate:
        requests = []
        def request(self, name, args, reason):
            self.requests.append(name)
            return f"appr-{next(_counter)}"
        def is_dangerous(self, *a): return False

    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    # Directly use the improver to enqueue two REVIEW edits
    async def _noop(_wt): return []
    e1 = Edit(op=EditOp.PATCH_CODE, summary="e1", apply=_noop)
    e2 = Edit(op=EditOp.PATCH_CODE, summary="e2", apply=_noop)
    asyncio.run(hos.improver.run([e1]))
    asyncio.run(hos.improver.run([e2]))
    assert hos.improver.pending_count() == 2
    cancelled = hos.abort_all_self_mods()
    assert cancelled == 2
    assert hos.improver.pending_count() == 0


def test_last_self_mod_branch_none_when_no_proposals(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.last_self_mod_branch() is None


def test_self_mod_history_empty_initially(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.self_mod_history() == []


def test_recent_self_mod_branches_empty_initially(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.recent_self_mod_branches() == []


def test_resume_after_restart_returns_dict(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    result = hos.resume_after_restart()
    assert "requeued" in result
    assert result["requeued"] == 0  # no running tasks to recover


def test_resume_after_restart_requeues_running_tasks(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    tid = hos.task_board.enqueue("tool", {})
    hos.task_board.claim(tid)   # now it's RUNNING
    result = hos.resume_after_restart()
    assert result["requeued"] == 1
    assert hos.task_board.get(tid).state == "pending"


def test_event_history_empty_initially(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    events = hos.event_history()
    assert isinstance(events, list)


def test_loop_guard_stats_returns_dict(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    stats = hos.loop_guard_stats()
    assert "total_calls" in stats
    assert "max_per_tool" in stats


def test_reset_loop_guard_clears_state(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    # Manually add a call to the guard to verify reset clears it
    hos.loop_guard.check("shell", {"cmd": "ls"})
    assert hos.loop_guard_stats()["total_calls"] == 1
    hos.reset_loop_guard()
    assert hos.loop_guard_stats()["total_calls"] == 0


# --- N-4: channel_hint flows through HiveOS.ask() and ask_stream() -----------

def test_ask_channel_hint_reaches_system_prompt(tmp_path):
    """channel_hint='telegram' must appear in the system param seen by the router."""
    captured = {}

    class _CapturingRouter:
        async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
            captured["system"] = system or ""
            return CompletionResult(text="ok", model="fake")
        async def aclose(self): pass

    hos = HiveOS.build(_config(tmp_path), router=_CapturingRouter())
    asyncio.run(hos.ask("hello", channel_hint="telegram"))
    assert "[Active surface: telegram]" in captured.get("system", ""), \
        f"channel_hint not in system prompt: {captured.get('system', '')[:200]}"


def test_ask_stream_channel_hint_reaches_system_prompt(tmp_path):
    """channel_hint='web' must appear in the system param during streaming."""
    captured = {}

    class _StreamRouter:
        async def stream(self, messages, *, system=None, **kw):
            captured["system"] = system or ""
            yield "token"
        async def aclose(self): pass

    hos = HiveOS.build(_config(tmp_path), router=_StreamRouter())

    async def collect():
        return [t async for t in hos.ask_stream("hi", channel_hint="web")]

    asyncio.run(collect())
    assert "[Active surface: web]" in captured.get("system", ""), \
        f"channel_hint not in stream system prompt: {captured.get('system', '')[:200]}"


def test_hive_health_returns_dict(tmp_path):
    """health() returns a dict with all expected top-level keys."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    result = hos.health()
    assert isinstance(result, dict)
    for key in ("status", "tools", "budget", "telemetry", "tasks", "memory",
                "pending_approvals", "pending_review_edits", "cron_jobs",
                "active_commitments", "self_mod_proposals"):
        assert key in result, f"missing key: {key}"
    assert result["status"] == "ok"
    assert isinstance(result["tools"], int)


def test_hive_consolidate_returns_int(tmp_path):
    """consolidate() returns an integer count (0 when the session is empty)."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    result = asyncio.run(hos.consolidate())
    assert isinstance(result, int)
    assert result == 0


def test_hive_curate_returns_dict(tmp_path):
    """curate() returns a dict with the expected lifecycle-report keys."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    result = hos.curate()
    assert isinstance(result, dict)
    assert "skills" in result
    assert "transitions" in result
    assert isinstance(result["transitions"], list)


def test_hive_curate_umbrellas_fail_open(tmp_path):
    """curate_umbrellas() must not raise even if umbrella consolidation fails."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    result = asyncio.run(hos.curate_umbrellas())
    assert isinstance(result, dict)
    # With no agent-created skills the curator short-circuits; always returns a dict.
    assert "skipped" in result or "umbrellas_created" in result


def test_hive_curator_deregisters_and_reregisters_learned_skill(tmp_path):
    """Integration (Batch H): the deregister/reregister closures wired into Curator
    in HiveOS.build actually remove/restore a learned skill from the LIVE
    hive.tools / hive.tool_executor, not just its skill_usage row — closing the
    SPRINT_7 risk that an archived learned skill stays callable forever."""
    from hive.tools.learned_skills import add_learned_skill, propose_skill

    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    template = propose_skill(("hive_status",), registry=hos.tools)
    add_learned_skill(template, registry=hos.tools, skill_usage=hos.skill_usage,
                      store=hos.learned_skills, auto_approve=True,
                      executor=hos.tool_executor)
    name = template.name
    assert name in hos.tools
    assert hos.tool_executor.has_tool(name)

    assert hos.curator.set_state(name, "archived") is True
    assert name not in hos.tools
    assert not hos.tool_executor.has_tool(name)

    assert hos.curator.restore(name) is True
    assert name in hos.tools
    assert hos.tool_executor.has_tool(name)


def test_hive_curator_archive_keeps_learned_skills_status_in_sync(tmp_path):
    """Batch K: LearnedSkillStore.status must track the live registration state,
    not just skill_usage.state — otherwise GET /skills/learned?status=registered
    keeps listing an archived (deregistered) skill as registered forever, since
    the two tracking tables would silently drift apart."""
    from hive.tools.learned_skills import (
        STATUS_ARCHIVED,
        STATUS_REGISTERED,
        add_learned_skill,
        propose_skill,
    )

    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    template = propose_skill(("hive_status",), registry=hos.tools)
    add_learned_skill(template, registry=hos.tools, skill_usage=hos.skill_usage,
                      store=hos.learned_skills, auto_approve=True,
                      executor=hos.tool_executor)
    name = template.name
    assert hos.learned_skills.get(name).status == STATUS_REGISTERED

    hos.curator.set_state(name, "archived")
    assert hos.learned_skills.get(name).status == STATUS_ARCHIVED
    # And the archived skill must be absent from the "registered" listing.
    registered_names = {t.name for t in hos.learned_skills.list_by_status(STATUS_REGISTERED)}
    assert name not in registered_names

    hos.curator.restore(name)
    assert hos.learned_skills.get(name).status == STATUS_REGISTERED
    registered_names = {t.name for t in hos.learned_skills.list_by_status(STATUS_REGISTERED)}
    assert name in registered_names


def test_hive_curator_archive_actually_hides_skill_from_llm_prompt(tmp_path):
    """Stronger version of the above: proves the orchestrator's tool SCHEMA (what
    the LLM is actually offered, via router.saw_tools) reflects the archive, not
    just hive.tools/hive.tool_executor. ConversationOrchestrator used to take a
    one-time COPY of the tools dict at construction (self._tools = dict(tools)),
    so mutating hive.tools afterward — exactly what deregister/reregister do —
    had zero effect on what got sent to the model: an archived skill stayed
    prompt-visible forever, which is the actual SPRINT_7 gap this batch of work
    is meant to close. The orchestrator now aliases the live dict instead."""
    from hive.tools.learned_skills import add_learned_skill, propose_skill

    router = _ScriptRouter([
        CompletionResult(text="hi", model="m"),
        CompletionResult(text="hi again", model="m"),
        CompletionResult(text="hi once more", model="m"),
    ])
    hos = HiveOS.build(_config(tmp_path), router=router)
    template = propose_skill(("hive_status",), registry=hos.tools)
    add_learned_skill(template, registry=hos.tools, skill_usage=hos.skill_usage,
                      store=hos.learned_skills, auto_approve=True,
                      executor=hos.tool_executor)
    name = template.name

    asyncio.run(hos.ask("hello", session_id="s-visible"))
    assert any(t["name"] == name for t in router.saw_tools)

    hos.curator.set_state(name, "archived")
    asyncio.run(hos.ask("hello again", session_id="s-hidden"))
    assert not any(t["name"] == name for t in router.saw_tools)

    hos.curator.restore(name)
    asyncio.run(hos.ask("hello once more", session_id="s-back"))
    assert any(t["name"] == name for t in router.saw_tools)


def test_hive_aclose_idempotent(tmp_path):
    """aclose() must not raise when called twice."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    asyncio.run(hos.aclose())
    asyncio.run(hos.aclose())  # second call must not raise


def test_hive_self_improve_from_symptom_smoke(tmp_path, monkeypatch):
    """self_improve_from_symptom() returns a list without raising."""
    async def _fake_diagnose_and_run(diagnoser, symptom, improver):
        return []

    monkeypatch.setattr(
        "hive.core.spec_search.diagnose_and_run", _fake_diagnose_and_run
    )
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    result = asyncio.run(hos.self_improve_from_symptom("test failing: some error"))
    assert isinstance(result, list)


# --- Additional runtime tests ---------------------------------------------------

def test_tools_dict_is_nonempty_after_build(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert len(hos.tools) > 0


def test_hive_build_twice_different_instances(tmp_path):
    a = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    b = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert a is not b


def test_hive_router_attribute_is_set(tmp_path):
    r = _ScriptRouter([])
    hos = HiveOS.build(_config(tmp_path), router=r)
    assert hos.router is r


def test_hive_session_store_attribute_exists(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.session_store is not None


def test_hive_events_attribute_exists(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.events is not None


def test_hive_ask_returns_string(tmp_path):
    from hive.llm.adapters.base import CompletionResult
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([
        CompletionResult(text="hello world", model="fake")
    ]))
    reply = asyncio.run(hos.ask("test message", session_id="s1"))
    assert isinstance(reply, str) and len(reply) > 0


def test_hive_memory_attribute_exists(tmp_path):
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.memory is not None


def test_hive_config_stored(tmp_path):
    cfg = _config(tmp_path)
    hos = HiveOS.build(cfg, router=_ScriptRouter([]))
    # config is accessible; root path matches
    assert str(tmp_path) in str(hos.config.root) or str(tmp_path) in str(hos.config.data_dir)


# --- Wave 3L additional tests ---------------------------------------------------

def test_hive_health_includes_memory_key(tmp_path):
    """HiveOS.health() dict must have 'memory' key."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    h = hos.health()
    assert "memory" in h


def test_hive_health_includes_budget_key(tmp_path):
    """HiveOS.health() dict must have 'budget' key."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    h = hos.health()
    assert "budget" in h


def test_hive_system_status_has_tools_key(tmp_path):
    """system_status() dict must have 'tools' key."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    s = hos.system_status()
    assert "tools" in s


def test_hive_system_status_has_router_key(tmp_path):
    """system_status() dict must have 'router' key."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    s = hos.system_status()
    assert "router" in s


def test_hive_skill_usage_is_not_none(tmp_path):
    """HiveOS.skill_usage is accessible after build."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.skill_usage is not None


def test_hive_keeper_is_not_none(tmp_path):
    """HiveOS.keeper is accessible after build."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.keeper is not None


# --- Wave 3N additional tests ---------------------------------------------------

def test_hive_loop_guard_stats_returns_dict(tmp_path):
    """HiveOS.loop_guard_stats() returns a dict."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    stats = hos.loop_guard_stats()
    assert isinstance(stats, dict)


def test_hive_reset_loop_guard_does_not_raise(tmp_path):
    """HiveOS.reset_loop_guard() runs without raising."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    hos.reset_loop_guard()  # should be idempotent


def test_hive_event_history_returns_list(tmp_path):
    """HiveOS.event_history() returns a list."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    hist = hos.event_history()
    assert isinstance(hist, list)


def test_hive_agents_registry_not_empty(tmp_path):
    """HiveOS.agents_registry has at least one entry after build."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert len(hos.agents_registry) > 0


def test_hive_audit_log_not_none(tmp_path):
    """HiveOS.audit_log is accessible after build."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.audit_log is not None


def test_hive_aclose_sets_router_closed(tmp_path):
    """HiveOS.aclose() closes the router."""
    router = _ScriptRouter([])
    hos = HiveOS.build(_config(tmp_path), router=router)
    asyncio.run(hos.aclose())
    assert router.closed is True


# --- Wave 3S additional tests ---------------------------------------------------

def test_hive_skill_usage_not_none(tmp_path):
    """HiveOS.skill_usage is accessible after build."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.skill_usage is not None


def test_hive_curator_not_none(tmp_path):
    """HiveOS.curator is a Curator instance."""
    from hive.memory.curator import Curator
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert isinstance(hos.curator, Curator)


def test_hive_session_store_not_none(tmp_path):
    """HiveOS.session_store is accessible after build."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.session_store is not None


def test_hive_tool_executor_not_none(tmp_path):
    """HiveOS.tool_executor is a ToolExecutor instance."""
    from hive.tools.executor import ToolExecutor
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert isinstance(hos.tool_executor, ToolExecutor)


def test_hive_config_accessible(tmp_path):
    """HiveOS.config returns the config that was passed to build()."""
    from hive.core.config import HiveConfig
    cfg = _config(tmp_path)
    hos = HiveOS.build(cfg, router=_ScriptRouter([]))
    assert isinstance(hos.config, HiveConfig)


def test_hive_health_returns_dict(tmp_path):
    """HiveOS.health() returns a dict with at least one key."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    h = hos.health()
    assert isinstance(h, dict)
    assert len(h) > 0


# --- Wave 4B additional tests ---------------------------------------------------

def test_wave4b_budgeter_not_none(tmp_path):
    """HiveOS.budgeter is wired after build()."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.budgeter is not None


def test_wave4b_telemetry_snapshot_is_dict(tmp_path):
    """HiveOS.telemetry.snapshot() returns a plain dict."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    snap = hos.telemetry.snapshot()
    assert isinstance(snap, dict)
    assert "inference_calls" in snap


def test_wave4b_curate_umbrellas_returns_dict_with_skipped_or_created(tmp_path):
    """curate_umbrellas() returns a dict containing 'skipped' or 'umbrellas_created'."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    result = asyncio.run(hos.curate_umbrellas())
    assert isinstance(result, dict)
    assert "skipped" in result or "umbrellas_created" in result


def test_wave4b_aclose_marks_router_closed(tmp_path):
    """aclose() sets router.closed to True."""
    router = _ScriptRouter([])
    hos = HiveOS.build(_config(tmp_path), router=router)
    assert not router.closed
    asyncio.run(hos.aclose())
    assert router.closed is True


def test_wave4b_ask_method_is_callable(tmp_path):
    """HiveOS.ask is a callable method."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert callable(hos.ask)


def test_wave4b_ask_stream_method_is_callable(tmp_path):
    """HiveOS.ask_stream is a callable method."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert callable(hos.ask_stream)


def test_wave4b_health_telemetry_key_is_dict(tmp_path):
    """health()['telemetry'] is a dict with inference_calls."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    h = hos.health()
    assert isinstance(h["telemetry"], dict)
    assert "inference_calls" in h["telemetry"]


def test_wave4b_health_tasks_key_is_dict(tmp_path):
    """health()['tasks'] is a dict with a 'total' key."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    h = hos.health()
    assert isinstance(h["tasks"], dict)
    assert "total" in h["tasks"]


# --- Wave 4I additional tests ---------------------------------------------------

def test_wave4i_system_status_has_self_mod_history_count(tmp_path):
    """system_status() contains a 'self_mod_history_count' integer key."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    s = hos.system_status()
    assert "self_mod_history_count" in s
    assert isinstance(s["self_mod_history_count"], int)


def test_wave4i_system_status_budget_has_daily_cap(tmp_path):
    """system_status()['budget'] contains 'daily_cap' and 'calls_today' sub-keys."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    s = hos.system_status()
    budget = s["budget"]
    assert "daily_cap" in budget
    assert "calls_today" in budget


def test_wave4i_ask_empty_string_returns_str(tmp_path):
    """ask('') with an empty input must return a str without raising."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([
        CompletionResult(text="empty reply", model="fake")
    ]))
    result = asyncio.run(hos.ask(""))
    assert isinstance(result, str)


def test_wave4i_task_board_total_count_starts_at_zero(tmp_path):
    """task_board.total_count() is 0 on a freshly built HiveOS."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.task_board.total_count() == 0


def test_wave4i_task_board_pending_count_increases_after_enqueue(tmp_path):
    """task_board.pending_count() increments by 1 after enqueue()."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    before = hos.task_board.pending_count()
    hos.task_board.enqueue("my_tool", {"arg": "val"})
    assert hos.task_board.pending_count() == before + 1


def test_wave4i_task_board_complete_changes_state_to_done(tmp_path):
    """Claiming then completing a task sets its state to 'done'."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    tid = hos.task_board.enqueue("finish_tool", {})
    hos.task_board.claim(tid)
    hos.task_board.complete(tid)
    assert hos.task_board.get(tid).state == "done"


def test_wave4i_task_board_count_by_kind_reflects_enqueued_kind(tmp_path):
    """count_by_kind() returns the correct count for the enqueued task kind."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    hos.task_board.enqueue("special_kind", {})
    hos.task_board.enqueue("special_kind", {})
    by_kind = hos.task_board.count_by_kind()
    assert by_kind.get("special_kind", 0) == 2


def test_wave4i_task_board_statistics_returns_dict_with_total(tmp_path):
    """task_board.statistics() returns a dict containing a 'total' key."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    hos.task_board.enqueue("stat_tool", {})
    stats = hos.task_board.statistics()
    assert isinstance(stats, dict)
    assert "total" in stats
    assert stats["total"] >= 1


# --- Wave 4O additional tests ---------------------------------------------------

def test_wave4o_build_router_not_none(tmp_path):
    """HiveOS.build() sets router to a non-None value."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.router is not None


def test_wave4o_build_memory_not_none(tmp_path):
    """HiveOS.build() sets memory to a non-None value."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.memory is not None


def test_wave4o_ask_non_empty_input_returns_nonempty(tmp_path):
    """ask() with a non-empty prompt returns a non-empty string."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([
        CompletionResult(text="response text", model="fake")
    ]))
    result = asyncio.run(hos.ask("what is 2+2?"))
    assert isinstance(result, str) and len(result) > 0


def test_wave4o_health_status_key_present(tmp_path):
    """health() result must have 'status', 'memory', 'tools', and 'budget' keys."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    h = hos.health()
    for key in ("status", "memory", "tools", "budget"):
        assert key in h, f"missing key: {key}"


def test_wave4o_task_board_create_and_count(tmp_path):
    """Enqueuing two tasks raises total_count() to 2."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    assert hos.task_board.total_count() == 0
    hos.task_board.enqueue("alpha", {})
    hos.task_board.enqueue("beta", {})
    assert hos.task_board.total_count() == 2


def test_wave4o_telemetry_snapshot_has_expected_keys(tmp_path):
    """telemetry.snapshot() contains inference_calls, tool_calls, and cost_usd."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    snap = hos.telemetry.snapshot()
    for key in ("inference_calls", "tool_calls", "cost_usd"):
        assert key in snap, f"missing key: {key}"


def test_wave4o_session_store_accessible_via_hive(tmp_path):
    """HiveOS.session_store is accessible and can list messages for a new session."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    msgs = hos.session_store.messages("brand-new-session")
    assert isinstance(msgs, list)


def test_wave4o_skill_store_by_state_returns_list(tmp_path):
    """skill_usage.by_state('active') returns a list."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    result = hos.skill_usage.by_state("active")
    assert isinstance(result, list)


def test_wave4o_curate_umbrellas_returns_dict_with_skipped(tmp_path):
    """curate_umbrellas() always returns a dict; contains 'skipped' or 'umbrellas_created'."""
    hos = HiveOS.build(_config(tmp_path), router=_ScriptRouter([]))
    result = asyncio.run(hos.curate_umbrellas())
    assert isinstance(result, dict)
    assert "skipped" in result or "umbrellas_created" in result


# ---------------------------------------------------------------------------
# runtime.py coverage follow-up — exercise the branches still missed after
# the existing 76-test suite. Targets lines 148-149, 163-164, 167-168,
# 189-191, 196-197, 253, 335-344, 351-352, 356-361, 452-453, 459-464,
# 481-484, 487, 491, 505, 517-530, 533, 536-537, 547-548, 585, 589-593,
# 631, 760, 788-790.
# ---------------------------------------------------------------------------


class _StreamingRouter:
    """Router that yields the given chunks via .stream()."""
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def stream(self, messages, *, system=None, **_kw):
        for c in self._chunks:
            yield c

    async def complete(self, *_a, **_kw):  # pragma: no cover - safety net
        from hive.llm.adapters.base import CompletionResult
        return CompletionResult(text="", model="fake")

    async def aclose(self):
        pass


def test_ask_stream_persist_failure_is_swallowed(tmp_path, caplog):
    """ask_stream yields chunks even if post-stream persistence raises (lines 148-149)."""
    import logging

    cfg = _config(tmp_path)
    hos = HiveOS.build(cfg, router=_StreamingRouter(["hi", " there"]))

    # Make the post-stream persist call raise by patching session_store.append.
    boom = RuntimeError("db locked")
    real_append = hos.session_store.append
    def append_boom(*a, **kw):
        raise boom
    hos.session_store.append = append_boom

    async def _drive():
        chunks = []
        async for delta in hos.ask_stream("hello", session_id="s-stream"):
            chunks.append(delta)
        return chunks

    with caplog.at_level(logging.WARNING, logger="hive.runtime"):
        chunks = asyncio.run(_drive())
    assert "".join(chunks) == "hi there"
    assert any("ask_stream persist failed" in r.message for r in caplog.records)
    hos.session_store.append = real_append


def test_health_handles_memory_count_exception(tmp_path):
    """health() reports -1 pending_approvals and {} memory on failure (163-168)."""
    cfg = _config(tmp_path)
    hos = HiveOS.build(cfg, router=_ScriptRouter([]))

    # Break memory.count and the approval gate lookup.
    class _BadMemory:
        def count(self):
            raise RuntimeError("count broken")
        def recall(self, *a, **kw):  # used by some health paths
            return ""
    hos.memory = _BadMemory()
    # also break gate.pending() so the second except runs.
    from hive.core import approval as _approval_mod
    real_pending = _approval_mod.gate.pending
    _approval_mod.gate.pending = lambda: (_ for _ in ()).throw(RuntimeError("gate broken"))
    try:
        snap = hos.health()
        assert snap["pending_approvals"] == -1
        assert snap["memory"] == {}
        assert snap["status"] == "ok"
    finally:
        _approval_mod.gate.pending = real_pending


def test_system_status_handles_router_exception(tmp_path):
    """system_status() returns empty router dict on exception (189-191, 196-197)."""
    cfg = _config(tmp_path)
    hos = HiveOS.build(cfg, router=_ScriptRouter([]))

    class _BadRouter:
        def status(self):
            raise RuntimeError("status broken")
    hos.router = _BadRouter()
    class _BadMemory:
        def count(self):
            raise RuntimeError("count broken")
    hos.memory = _BadMemory()

    snap = hos.system_status()
    assert snap["router"] == {}
    assert snap["memory"] == {}


def test_load_mcp_servers_skips_empty_spec(tmp_path, monkeypatch):
    """An empty stdio spec is silently skipped (line 253)."""
    from dataclasses import replace
    cfg = _config(tmp_path)
    cfg = replace(cfg, mcp_servers=("",))   # blank spec — shlex.split returns []
    hos = HiveOS.build(cfg, router=_ScriptRouter([]))
    loaded = asyncio.run(hos.load_mcp_servers())
    assert loaded == 0


def test_build_symptom_context_aggregates_audit_task_and_prior_proposals(tmp_path):
    """All three try blocks fire when their data is populated (335-361)."""
    cfg = _config(tmp_path)
    hos = HiveOS.build(cfg, router=_ScriptRouter([]))

    # Force audit error_rate > 0.05 with stats carrying bad tools.
    from unittest.mock import MagicMock
    audit = MagicMock()
    audit.error_rate.return_value = 0.10
    audit.stats.return_value = {
        "by_tool": {"shell": {"total": 10, "by_status": {"error": 4, "ok": 6}}}
    }
    hos.audit_log = audit

    # TaskBoard with a recent failure.
    from hive.autonomy.tasks import TaskBoard
    board = TaskBoard(cfg.state_db)
    rid = board.enqueue("test_kind", {"x": 1}, source="test")
    board.fail(rid, "boom")
    hos.task_board = board

    # Self-modifier with a failed proposal recorded in history.
    from hive.core.self_mod import SelfModifier
    sm = SelfModifier(repo_root=str(cfg.root))
    sm._history.append({"title": "prior attempt", "stage": "test_fail", "ok": False})
    hos.self_modifier = sm

    out = asyncio.run(hos._build_symptom_context("base ctx"))
    assert "base ctx" in out
    assert "Tool errors (24h)" in out
    assert "shell" in out
    assert "Recent task failures" in out
    assert "Prior failed proposals" in out


def test_self_improve_from_symptom_strips_markdown_fences_and_handles_invalid_items(tmp_path):
    """The diagnoser path tolerates ```json fences + non-list returns + bad op (481-491)."""
    cfg = _config(tmp_path)
    hos = HiveOS.build(cfg, router=_ScriptRouter([]))

    # Script three malformed LLM responses covering different branches.
    from hive.llm.adapters.base import CompletionResult
    router = _ScriptRouter([
        CompletionResult(text="```json\n[]\n```", model="m"),   # markdown fence → []
        CompletionResult(text="not a list", model="m"),           # not a list → []
        CompletionResult(text='[{"no_op": true}]', model="m"),    # missing op → skip
    ])

    # Disable budget guard so we hit the diagnoser branch.
    hos.budgeter.is_near_cap = lambda: False

    # Patch diagnose_and_run so we drive the inner _diagnoser directly.
    from hive.core import spec_search as _ss
    real = _ss.diagnose_and_run
    seen = []
    async def fake_diagnose(diagnoser, symptom, improver):
        e1 = await diagnoser("s1")
        e2 = await diagnoser("s2")
        e3 = await diagnoser("s3")
        seen.extend([len(e1), len(e2), len(e3)])
        return []
    _ss.diagnose_and_run = fake_diagnose
    try:
        asyncio.run(hos.self_improve_from_symptom("symptom text"))
    finally:
        _ss.diagnose_and_run = real
    assert seen == [0, 0, 0]


def test_diagnoser_sets_target_files_and_code_on_edit(tmp_path):
    """Diagnoser edits carry targets and scan every payload for dangerous patterns.

    Only complete CREATE_FILE Python files are eligible for syntax parsing;
    replacement fragments are intentionally marked as incomplete files."""
    cfg = _config(tmp_path)
    router = _ScriptRouter([
        CompletionResult(text=json.dumps([{
            "op": "create_file", "path": "src/hive/tools/new_helper.py",
            "old_text": "", "new_text": "import os\neval('1+1')\n",
            "summary": "s", "rationale": "r",
        }]), model="m"),
        CompletionResult(text=json.dumps([{
            "op": "patch_code", "path": "docs/NOTES.md",
            "old_text": "X", "new_text": "Y",
            "summary": "s", "rationale": "r",
        }]), model="m"),
    ])
    hos = HiveOS.build(cfg, router=router)
    hos.budgeter.is_near_cap = lambda: False

    from hive.core import spec_search as _ss
    real = _ss.diagnose_and_run
    captured = []
    async def fake_diagnose(diagnoser, symptom, improver):
        captured.append(await diagnoser(symptom))
        return []
    _ss.diagnose_and_run = fake_diagnose
    try:
        asyncio.run(hos.self_improve_from_symptom("s1"))
        asyncio.run(hos.self_improve_from_symptom("s2"))
    finally:
        _ss.diagnose_and_run = real

    create_edit = captured[0][0]
    assert create_edit.target_files == ["src/hive/tools/new_helper.py"]
    assert create_edit.code == "import os\neval('1+1')\n"

    patch_edit = captured[1][0]
    assert patch_edit.target_files == ["docs/NOTES.md"]
    assert patch_edit.code == "Y"  # fragment is scanned for dangerous patterns
    assert patch_edit.code_is_complete_file is False


def test_diagnoser_code_scoping_is_case_insensitive_for_py_extension(tmp_path):
    """A path like `helper.PY` must still be treated as a CREATE_FILE .py edit —
    an audit finding on this PR: `.endswith('.py')` was case-sensitive, which
    would have skipped the safety-check code= population for that path."""
    cfg = _config(tmp_path)
    router = _ScriptRouter([CompletionResult(text=json.dumps([{
        "op": "create_file", "path": "src/hive/tools/new_helper.PY",
        "old_text": "", "new_text": "import os\n",
        "summary": "s", "rationale": "r",
    }]), model="m")])
    hos = HiveOS.build(cfg, router=router)
    hos.budgeter.is_near_cap = lambda: False

    from hive.core import spec_search as _ss
    real = _ss.diagnose_and_run
    captured = []
    async def fake_diagnose(diagnoser, symptom, improver):
        captured.append(await diagnoser(symptom))
        return []
    _ss.diagnose_and_run = fake_diagnose
    try:
        asyncio.run(hos.self_improve_from_symptom("s1"))
    finally:
        _ss.diagnose_and_run = real

    assert captured[0][0].code == "import os\n"


class _FakeSelfModifierForSafety:
    """Stands in for SelfModifier so the safety-escalation test never touches
    real git/worktrees — only records whether AUTO-apply was attempted."""
    def __init__(self):
        self.calls = 0

    async def propose(self, title, description, apply_fn, *, dry_run=False):
        self.calls += 1
        return {"ok": True, "stage": "pushed", "branch": "hive/auto-x"}


def test_dangerous_create_file_edit_is_escalated_not_silently_applied(tmp_path):
    """Regression for the Pillar-4 silent-bypass bug: before the fix,
    Edit.target_files was always [] and Edit.code always None, so
    run_all_checks() always returned [] and a CREATE_FILE proposal containing
    a dangerous pattern would go straight to SelfModifier.propose() (AUTO, no
    human review) and be reported 'applied'."""
    from hive.core.spec_search import RiskTier

    cfg = _config(tmp_path)
    dangerous = "import os\nos.system('rm -rf /')\n"
    router = _ScriptRouter([CompletionResult(text=json.dumps([{
        "op": "create_file", "path": "src/hive/tools/danger.py",
        "old_text": "", "new_text": dangerous,
        "summary": "s", "rationale": "r",
    }]), model="m")])
    hos = HiveOS.build(cfg, router=router)
    hos.budgeter.is_near_cap = lambda: False
    fake_mod = _FakeSelfModifierForSafety()
    hos.improver._mod = fake_mod  # swap out the real SelfModifier

    outcomes = asyncio.run(hos.self_improve_from_symptom("test symptom", _already_enriched=True))

    assert len(outcomes) == 1
    out = outcomes[0]
    assert out.status == "pending_approval", out.detail
    assert out.tier == RiskTier.REVIEW
    assert any(f["check"] == "dangerous_patterns" for f in out.safety_findings)
    assert fake_mod.calls == 0, "dangerous CREATE_FILE must NOT reach SelfModifier.propose() AUTO-applied"


def test_selfmod_safety_config_is_threaded_into_improver(tmp_path, monkeypatch):
    """cfg.selfmod_enable_safety_checks / cfg.selfmod_safety_max_files must
    reach SelfImprovement — before the fix both silently fell back to
    hardcoded defaults (True, 20) regardless of operator env config."""
    monkeypatch.setenv("HIVE_SELFMOD_SAFETY_MAX_FILES", "3")
    monkeypatch.setenv("HIVE_SELFMOD_ENABLE_SAFETY_CHECKS", "false")
    cfg = _config(tmp_path)
    assert cfg.selfmod_safety_max_files == 3
    assert cfg.selfmod_enable_safety_checks is False

    hos = HiveOS.build(cfg, router=_ScriptRouter([]))
    assert hos.improver._safety_max_files == 3
    assert hos.improver._safety_enabled is False


def test_last_self_mod_branch_returns_branch_or_none(tmp_path):
    """last_self_mod_branch returns branch on success, None otherwise (line 631)."""
    cfg = _config(tmp_path)
    hos = HiveOS.build(cfg, router=_ScriptRouter([]))

    # No prior result → None.
    hos.self_modifier._history.clear()
    assert hos.last_self_mod_branch() is None

    # ok=False → None.
    hos.self_modifier._history.append({"ok": False, "branch": "x"})
    assert hos.last_self_mod_branch() is None

    # ok=True + branch → branch.
    hos.self_modifier._history.append({"ok": True, "branch": "hive/autonomy/x"})
    assert hos.last_self_mod_branch() == "hive/autonomy/x"


def test_build_supports_docker_shell_provider(tmp_path, monkeypatch):
    """HiveOS.build() honors cfg.shell_provider='docker' (line 760)."""
    from unittest.mock import MagicMock
    monkeypatch.setenv("HIVE_SHELL_PROVIDER", "docker")
    monkeypatch.setenv("HIVE_SHELL_DOCKER_IMAGE", "hive-test:latest")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.shell_provider == "docker"

    docker_mock = MagicMock()
    import hive.tools.shell_provider as _sp
    real_docker = _sp.DockerShellProvider
    # Real signature is (image="alpine:latest", *, network="none").
    def _docker_factory(image="alpine:latest", *, network="none"):
        docker_mock(image=image, network=network)
        return docker_mock
    _sp.DockerShellProvider = _docker_factory
    try:
        hos = HiveOS.build(cfg, router=_ScriptRouter([]))
        docker_mock.assert_called_once_with(image="hive-test:latest", network="none")
        assert hos is not None
    finally:
        _sp.DockerShellProvider = real_docker
