"""P8 — budgeter gate, observability (telemetry/audit), self_mod dry-run, heartbeat tick."""
from __future__ import annotations

import asyncio

from hive.core.budgeter import Budgeter
from hive.core.events import EventBus, EventType
from hive.core.self_mod import SelfModifier
from hive.observability.audit import AuditLog
from hive.observability.telemetry import Telemetry


# --- budgeter ------------------------------------------------------------------

def test_budgeter_gate_blocks_at_cap():
    b = Budgeter(daily_cap=2)
    assert b.gate() == (True, "")
    b.record_call(); b.record_call()
    ok, why = b.gate()
    assert ok is False and "daily cap" in why
    assert b.snapshot()["calls_today"] == 2


def test_budgeter_rolls_over_day():
    t = [1000.0]
    b = Budgeter(daily_cap=1, clock=lambda: t[0])
    b.record_call()
    assert b.gate()[0] is False
    t[0] += 86_400 * 2          # next day
    assert b.gate()[0] is True


def test_budgeter_is_near_cap():
    b = Budgeter(daily_cap=10)
    assert b.is_near_cap() is False
    for _ in range(9):
        b.record_call()
    assert b.is_near_cap() is True  # 9/10 >= 0.9
    assert b.is_near_cap(threshold=1.0) is False  # 9/10 < 1.0
    b.record_call()
    assert b.is_near_cap(threshold=1.0) is True  # 10/10 == 1.0


def test_budgeter_reset_daily():
    b = Budgeter(daily_cap=5)
    b.record_call()
    b.record_call()
    assert b.snapshot()["calls_today"] == 2
    b.reset_daily()
    assert b.snapshot()["calls_today"] == 0
    assert b.snapshot()["cost_today_usd"] == 0.0


def test_budgeter_remaining_calls():
    b = Budgeter(daily_cap=10)
    assert b.remaining_calls() == 10
    b.record_call()
    b.record_call()
    assert b.remaining_calls() == 8
    # overshoot: capped at 0
    for _ in range(15):
        b.record_call()
    assert b.remaining_calls() == 0


def test_budgeter_calls_per_hour_zero_with_no_calls():
    b = Budgeter()
    assert b.calls_per_hour() == 0.0


def test_budgeter_calls_per_hour_positive_after_calls():
    import time
    # Use a clock that places us at mid-day UTC so hours_elapsed is meaningful
    now_ts = [86400.0 * 1000 + 12 * 3600]  # midnight offset 12h into a day
    b = Budgeter(clock=lambda: now_ts[0])
    for _ in range(12):
        b.record_call()
    rate = b.calls_per_hour()
    assert rate > 0.0


def test_budgeter_cost_per_call_zero_no_calls():
    b = Budgeter()
    assert b.cost_per_call() == 0.0


def test_budgeter_cost_per_call_accumulates():
    b = Budgeter()
    b.record_usage({"model": "m", "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.02})
    b.record_usage({"model": "m", "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.02})
    b.record_call()
    b.record_call()
    cpc = b.cost_per_call()
    assert abs(cpc - 0.02) < 1e-6


def test_budgeter_warning_status_none_when_healthy():
    b = Budgeter(daily_cap=100, warn_pct=70.0)
    b.record_call()  # 1/100 = 1% used → healthy
    assert b.warning_status() is None


def test_budgeter_warning_status_triggers_near_cap():
    b = Budgeter(daily_cap=10, warn_pct=90.0)
    for _ in range(9):  # 90% of cap
        b.record_call()
    w = b.warning_status()
    assert w is not None
    assert w["near_cap"] is True
    assert "secs_til_reset" in w


# --- telemetry (EventBus subscriber) -------------------------------------------

def test_telemetry_counts_from_bus():
    bus = EventBus()
    tel = Telemetry().attach(bus)
    bus.publish(EventType.INFERENCE_END, {"model": "MiniMax-M3", "output_tokens": 12})
    bus.publish(EventType.INFERENCE_END, {"model": "MiniMax-M3", "output_tokens": 8})
    bus.publish(EventType.TOOL_CALL_END, {"tool": "shell"})
    snap = tel.snapshot()
    assert snap["inference_calls"] == 2 and snap["output_tokens"] == 20
    assert snap["tool_calls"] == 1 and snap["by_model"]["MiniMax-M3"] == 2


def test_telemetry_reset_zeros_all_counters():
    tel = Telemetry()
    tel.inference_calls = 5
    tel.tool_calls = 3
    tel.selfmod_attempts = 1
    tel.by_model = {"MiniMax-M3": 5}
    tel.reset()
    snap = tel.snapshot()
    assert snap["inference_calls"] == 0
    assert snap["tool_calls"] == 0
    assert snap["selfmod_attempts"] == 0
    assert snap["by_model"] == {}


# --- audit log (SQLite sink) ---------------------------------------------------

def test_audit_log_records_and_reads(tmp_path):
    a = AuditLog(tmp_path / "audit.sqlite")
    a.record({"tool": "shell", "status": "ok", "args": {"cmd": "echo hi"}})
    a.record({"tool": "deploy", "status": "pending_approval", "args": {}})
    recent = a.recent()
    assert {r["tool"] for r in recent} == {"shell", "deploy"}


def test_telemetry_tracks_selfmod_outcomes():
    bus = EventBus()
    tel = Telemetry().attach(bus)
    from hive.core.events import EventType
    bus.publish(EventType.SELFMOD_END, {"ok": True, "stage": "pushed"})
    bus.publish(EventType.SELFMOD_END, {"ok": True, "stage": "pushed"})
    bus.publish(EventType.SELFMOD_END, {"ok": False, "stage": "test"})
    snap = tel.snapshot()
    assert snap["selfmod_attempts"] == 3
    assert snap["selfmod_succeeded"] == 2
    assert snap["selfmod_failed"] == 1


def test_telemetry_selfmod_success_rate_zero_no_attempts():
    tel = Telemetry()
    assert tel.selfmod_success_rate() == 0.0


def test_telemetry_selfmod_success_rate_mixed():
    bus = EventBus()
    tel = Telemetry().attach(bus)
    from hive.core.events import EventType
    bus.publish(EventType.SELFMOD_END, {"ok": True})
    bus.publish(EventType.SELFMOD_END, {"ok": False})
    bus.publish(EventType.SELFMOD_END, {"ok": False})
    assert abs(tel.selfmod_success_rate() - 1 / 3) < 0.001


def test_telemetry_top_model_none_when_no_calls():
    tel = Telemetry()
    assert tel.top_model() is None


def test_telemetry_top_model_returns_most_used():
    bus = EventBus()
    tel = Telemetry().attach(bus)
    from hive.core.events import EventType
    bus.publish(EventType.INFERENCE_END, {"model": "mini", "output_tokens": 10})
    bus.publish(EventType.INFERENCE_END, {"model": "mini", "output_tokens": 10})
    bus.publish(EventType.INFERENCE_END, {"model": "opus", "output_tokens": 10})
    assert tel.top_model() == "mini"


def test_telemetry_total_tokens_zero():
    tel = Telemetry()
    assert tel.total_tokens() == 0


def test_telemetry_total_tokens_accumulates():
    bus = EventBus()
    tel = Telemetry().attach(bus)
    from hive.core.events import EventType
    bus.publish(EventType.INFERENCE_END, {"model": "m", "input_tokens": 100, "output_tokens": 50})
    bus.publish(EventType.INFERENCE_END, {"model": "m", "input_tokens": 200, "output_tokens": 75})
    assert tel.total_tokens() == 425


def test_audit_log_prune_respects_max_rows(tmp_path):
    a = AuditLog(tmp_path / "audit.sqlite", max_rows=3)
    for i in range(6):
        a.record({"tool": f"tool{i}", "status": "ok", "args": {}})
    # After 6 records with max_rows=3 each record triggers a prune;
    # at most 3 rows should remain.
    recent = a.recent(limit=100)
    assert len(recent) <= 3


def test_audit_log_explicit_prune(tmp_path):
    a = AuditLog(tmp_path / "audit.sqlite", max_rows=100)
    for i in range(10):
        a.record({"tool": f"t{i}", "status": "ok", "args": {}})
    deleted = a.prune(max_rows=5)
    assert deleted == 5
    assert len(a.recent(limit=100)) == 5


# --- self_mod (dry-run + protected refusal) ------------------------------------

def _fake_runner(script):
    async def run(cmd, cwd=None):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        for key, rc, out in script:
            if key in cmd_str:
                return rc, out
        return 0, ""
    return run


def test_self_mod_dry_run_passes_without_push():
    runner = _fake_runner([("rev-parse", 0, "abc123\n"), ("worktree add", 0, ""),
                           ("git diff --name-only", 0, "src/hive/llm/router.py\n"),
                           ("git ls-files --others", 0, ""),
                           ("pytest", 0, "1 passed"), ("worktree remove", 0, "")])
    pushed = []

    async def apply_fn(wt):
        return ["src/hive/llm/router.py"]            # non-protected change

    sm = SelfModifier(run=runner, test_cmd="python -m pytest -q")
    res = asyncio.run(sm.propose("t", "d", apply_fn, dry_run=True))
    assert res["ok"] is True and res["stage"] == "dry_run"
    assert res["last_good"] == "abc123"


def test_self_mod_refuses_protected_paths():
    runner = _fake_runner([("rev-parse", 0, "abc\n"), ("worktree add", 0, ""),
                           ("worktree remove", 0, "")])

    async def apply_fn(wt):
        return ["config/SOUL.md"]                    # PROTECTED

    sm = SelfModifier(run=runner)
    res = asyncio.run(sm.propose("t", "d", apply_fn, dry_run=True))
    assert res["ok"] is False and res["stage"] == "protected"


def test_self_mod_test_failure_stays_on_last_good():
    runner = _fake_runner([("rev-parse", 0, "good\n"), ("worktree add", 0, ""),
                           ("git diff --name-only", 0, "src/hive/x.py\n"),
                           ("git ls-files --others", 0, ""),
                           ("pytest", 1, "FAILED test"), ("worktree remove", 0, "")])

    async def apply_fn(wt):
        return ["src/hive/x.py"]

    sm = SelfModifier(run=runner, test_cmd="python -m pytest -q")
    res = asyncio.run(sm.propose("t", "d", apply_fn))
    assert res["ok"] is False and res["stage"] == "test" and res["last_good"] == "good"


# --- heartbeat tick ------------------------------------------------------------

# --- budgeter: forecast, by_model snapshot, credit warning --------------------

def test_budgeter_forecast_zero_calls():
    b = Budgeter(daily_cap=100)
    f = b.forecast()
    assert f["calls_today"] == 0
    assert f["remaining_calls"] == 100
    assert f["pct_used"] == 0.0
    assert f["days_remaining"] is None


def test_budgeter_forecast_after_calls():
    b = Budgeter(daily_cap=10)
    b.record_call()
    b.record_call()
    f = b.forecast()
    assert f["calls_today"] == 2
    assert f["remaining_calls"] == 8
    assert f["days_remaining"] == 4.0  # 8 remaining / 2 today-rate


def test_budgeter_snapshot_by_model():
    b = Budgeter()
    b.record_usage({"model": "mini", "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01})
    b.record_usage({"model": "opus", "input_tokens": 200, "output_tokens": 100, "cost_usd": 0.05})
    snap = b.snapshot()
    assert "mini" in snap["by_model"]
    assert "opus" in snap["by_model"]
    assert snap["by_model"]["mini"]["input"] == 100
    assert snap["by_model"]["opus"]["cost_usd"] == 0.05


def test_budgeter_warning_status_triggers_on_credit_warn():
    b = Budgeter(daily_cap=1000, warn_pct=50.0)
    b._used_pct = 60.0  # simulate polled credit at 60% consumed
    w = b.warning_status()
    assert w is not None
    assert w["credit_pct_used"] == 60.0


def test_budgeter_allows_when_under_cap():
    b = Budgeter(daily_cap=10)
    for _ in range(5):
        b.record_call()
    ok, why = b.gate()
    assert ok is True
    assert why == ""


def test_budgeter_blocks_when_at_cap():
    b = Budgeter(daily_cap=3)
    b.record_call()
    b.record_call()
    b.record_call()
    ok, why = b.gate()
    assert ok is False
    assert "daily cap" in why


def test_budgeter_resets_on_new_day():
    t = [0.0]
    b = Budgeter(daily_cap=2, clock=lambda: t[0])
    b.record_call()
    b.record_call()
    assert b.gate()[0] is False
    t[0] += 86_400 + 1  # next calendar day
    assert b.gate()[0] is True


def test_budgeter_remaining_decrements():
    b = Budgeter(daily_cap=5)
    assert b.remaining_calls() == 5
    b.record_call()
    assert b.remaining_calls() == 4
    b.record_call()
    assert b.remaining_calls() == 3


# --- EventBus: subscriber isolation, unsubscribe_all, subscribe_once ----------

def test_eventbus_subscriber_isolation():
    bus = EventBus()
    results = []

    def bad_sub(_event):
        raise RuntimeError("subscriber failure")

    def good_sub(event):
        results.append(event.data.get("x"))

    bus.subscribe(EventType.TOOL_CALL_END, bad_sub)
    bus.subscribe(EventType.TOOL_CALL_END, good_sub)
    bus.publish(EventType.TOOL_CALL_END, {"x": 42})
    assert results == [42]


def test_eventbus_unsubscribe_all():
    bus = EventBus()
    calls = []
    bus.subscribe(EventType.INFERENCE_END, lambda e: calls.append(1))
    bus.subscribe(EventType.INFERENCE_END, lambda e: calls.append(2))
    assert bus.subscriber_count(EventType.INFERENCE_END) == 2
    removed = bus.unsubscribe_all(EventType.INFERENCE_END)
    assert removed == 2
    bus.publish(EventType.INFERENCE_END, {})
    assert calls == []


def test_eventbus_subscribe_once_fires_once():
    bus = EventBus()
    calls = []
    bus.subscribe_once(EventType.BUDGET_BLOCK, lambda e: calls.append(e.data.get("v")))
    bus.publish(EventType.BUDGET_BLOCK, {"v": 1})
    bus.publish(EventType.BUDGET_BLOCK, {"v": 2})
    assert calls == [1]


def test_eventbus_history_recording():
    bus = EventBus(record_history=True)
    bus.publish(EventType.INFERENCE_END, {"model": "m"})
    bus.publish(EventType.TOOL_CALL_END, {"tool": "t"})
    h = bus.history()
    assert len(h) == 2
    assert h[0].event_type == EventType.INFERENCE_END


def test_eventbus_history_count_and_clear():
    bus = EventBus(record_history=True)
    bus.publish(EventType.INFERENCE_END, {})
    bus.publish(EventType.INFERENCE_END, {})
    assert bus.history_count() == 2
    cleared = bus.clear_history()
    assert cleared == 2
    assert bus.history_count() == 0


def test_eventbus_subscriber_count():
    bus = EventBus()
    assert bus.subscriber_count(EventType.AGENT_TURN_END) == 0
    bus.subscribe(EventType.AGENT_TURN_END, lambda e: None)
    bus.subscribe(EventType.AGENT_TURN_END, lambda e: None)
    assert bus.subscriber_count(EventType.AGENT_TURN_END) == 2
    assert bus.total_subscribers() >= 2


def test_eventbus_history_by_type():
    bus = EventBus(record_history=True)
    bus.publish(EventType.INFERENCE_END, {})
    bus.publish(EventType.INFERENCE_END, {})
    bus.publish(EventType.TOOL_CALL_END, {})
    counts = bus.history_by_type()
    assert counts["inference_end"] == 2
    assert counts["tool_call_end"] == 1


def test_eventbus_recent_events():
    bus = EventBus(record_history=True)
    bus.publish(EventType.INFERENCE_END, {"model": "m1"})
    bus.publish(EventType.INFERENCE_END, {"model": "m2"})
    recent = bus.recent_events(n=1)
    assert len(recent) == 1
    assert recent[0]["data"]["model"] == "m2"


# --- heartbeat tick ------------------------------------------------------------

def test_heartbeat_tick_plans_dispatches_consolidates(tmp_path):
    import json
    from hive.core.config import HiveConfig
    from hive.llm.adapters.base import CompletionResult
    from hive.runtime import HiveOS
    from hive.autonomy.heartbeat import Heartbeat

    plan = [{"task": "check disk", "tool": "shell", "args": {"cmd": "echo ok"}, "reason": "r"}]

    class _Router:
        async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
            return CompletionResult(text="```json\n" + json.dumps(plan) + "\n```", model="m")
        async def aclose(self):
            pass

    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    # Exercises tick()'s plan/dispatch mechanics, not the P0 autonomy gate
    # (default-off) - enable it so the tick actually runs.
    object.__setattr__(cfg, "autonomy_enabled", True)
    object.__setattr__(cfg, "approver_key", "test-approver-key")
    hive = HiveOS.build(cfg, router=_Router())
    hb = Heartbeat(hive, goals=["stay healthy"])
    summary = asyncio.run(hb.tick())
    assert summary["planned"] == 1 and summary["dispatched"] == 1


# --- EventBus additional edge cases ----------------------------------------------

def test_eventbus_publish_error_in_subscriber_does_not_propagate():
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    ET = EventType.TOOL_CALL_START

    def bad_cb(event):
        raise RuntimeError("subscriber failure")

    results = []
    bus.subscribe(ET, bad_cb)
    bus.subscribe(ET, lambda e: results.append(e))
    bus.publish(ET, {"tool": "x"})
    # bad_cb raised but the second subscriber still fired
    assert len(results) == 1


def test_eventbus_total_subscribers_counts_all():
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    bus.subscribe(EventType.TOOL_CALL_START, lambda e: None)
    bus.subscribe(EventType.TOOL_CALL_START, lambda e: None)
    bus.subscribe(EventType.MEMORY_STORE, lambda e: None)
    assert bus.total_subscribers() == 3


def test_eventbus_history_not_recorded_by_default():
    from hive.core.events import EventBus, EventType
    bus = EventBus()  # record_history=False by default
    bus.publish(EventType.TOOL_CALL_START, {})
    assert bus.history_count() == 0


def test_eventbus_history_max_rolls_over():
    from hive.core.events import EventBus, EventType
    bus = EventBus(record_history=True, history_max=3)
    for i in range(5):
        bus.publish(EventType.TOOL_CALL_START, {"i": i})
    assert bus.history_count() == 3


def test_eventbus_publish_with_no_subscribers_does_not_raise():
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    bus.publish(EventType.MEMORY_STORE, {"key": "val"})  # no subscribers


# --- Budgeter additional edge cases ----------------------------------------------

def test_budgeter_gate_returns_tuple():
    from hive.core.budgeter import Budgeter
    b = Budgeter(daily_cap=3000)
    allowed, msg = b.gate()
    assert isinstance(allowed, bool)
    assert isinstance(msg, str)


def test_budgeter_record_call_increments_count():
    from hive.core.budgeter import Budgeter
    b = Budgeter(daily_cap=3000)
    snap_before = b.snapshot()
    b.record_call()
    snap_after = b.snapshot()
    assert snap_after["calls_today"] == snap_before["calls_today"] + 1


def test_budgeter_remaining_calls_decrements():
    from hive.core.budgeter import Budgeter
    b = Budgeter(daily_cap=10)
    before = b.remaining_calls()
    b.record_call()
    assert b.remaining_calls() == before - 1


# --- Additional Budgeter tests ---------------------------------------------------

def test_budgeter_is_near_cap_false_initially():
    from hive.core.budgeter import Budgeter
    b = Budgeter(daily_cap=1000)
    assert b.is_near_cap() is False


def test_budgeter_snapshot_has_required_keys():
    from hive.core.budgeter import Budgeter
    b = Budgeter(daily_cap=500)
    snap = b.snapshot()
    assert "calls_today" in snap and "daily_cap" in snap


def test_budgeter_forecast_returns_dict():
    from hive.core.budgeter import Budgeter
    b = Budgeter(daily_cap=100)
    f = b.forecast()
    assert isinstance(f, dict)


def test_budgeter_reset_daily_clears_calls():
    from hive.core.budgeter import Budgeter
    b = Budgeter(daily_cap=100)
    b.record_call()
    b.reset_daily()
    assert b.snapshot()["calls_today"] == 0


def test_budgeter_warning_status_returns_none_or_string():
    from hive.core.budgeter import Budgeter
    b = Budgeter(daily_cap=100)
    ws = b.warning_status()
    assert ws is None or isinstance(ws, str)


# --- Additional EventBus tests ---------------------------------------------------

def test_eventbus_history_by_type_after_publish():
    from hive.core.events import EventBus, EventType
    bus = EventBus(record_history=True)
    bus.publish(EventType.TOOL_CALL_START, {"x": 1})
    by_type = bus.history_by_type()
    # history_by_type returns dict keyed by string or EventType
    key = EventType.TOOL_CALL_START.value if isinstance(EventType.TOOL_CALL_START, EventType) else EventType.TOOL_CALL_START
    assert key in by_type or EventType.TOOL_CALL_START in by_type


def test_eventbus_recent_events_returns_list():
    from hive.core.events import EventBus, EventType
    bus = EventBus(record_history=True)
    for i in range(5):
        bus.publish(EventType.TOOL_CALL_START, {"i": i})
    recent = bus.recent_events()
    assert isinstance(recent, list) and len(recent) == 5


def test_eventbus_history_count_zero_initially():
    from hive.core.events import EventBus
    bus = EventBus(record_history=True)
    assert bus.history_count() == 0


def test_eventbus_subscribe_count_after_multiple():
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    bus.subscribe(EventType.TOOL_CALL_START, lambda e: None)
    bus.subscribe(EventType.TOOL_CALL_END, lambda e: None)
    assert bus.total_subscribers() >= 2


# --- 6 new tests ---------------------------------------------------------------

def test_eventbus_subscriber_receives_published_event():
    """A subscriber registered for an event type receives the published payload."""
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    received = []
    bus.subscribe(EventType.TOOL_CALL_START, received.append)
    bus.publish(EventType.TOOL_CALL_START, {"tool": "shell"})
    assert len(received) == 1
    assert received[0].data["tool"] == "shell"


def test_eventbus_subscribe_once_fires_exactly_once():
    """subscribe_once handler is called exactly once even when the event fires twice."""
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    calls = []
    bus.subscribe_once(EventType.TOOL_CALL_END, calls.append)
    bus.publish(EventType.TOOL_CALL_END, {"n": 1})
    bus.publish(EventType.TOOL_CALL_END, {"n": 2})
    assert len(calls) == 1


def test_eventbus_unsubscribe_all_removes_handlers():
    """unsubscribe_all() removes all handlers for a given event type."""
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    calls = []
    bus.subscribe(EventType.TOOL_CALL_START, calls.append)
    bus.subscribe(EventType.TOOL_CALL_START, calls.append)
    bus.unsubscribe_all(EventType.TOOL_CALL_START)
    bus.publish(EventType.TOOL_CALL_START, {})
    assert calls == []


def test_eventbus_clear_history_resets_count():
    """clear_history() resets history_count() to zero after events were published."""
    from hive.core.events import EventBus, EventType
    bus = EventBus(record_history=True)
    bus.publish(EventType.MEMORY_STORE, {"key": "a"})
    bus.publish(EventType.MEMORY_STORE, {"key": "b"})
    assert bus.history_count() == 2
    bus.clear_history()
    assert bus.history_count() == 0


def test_budgeter_forecast_remaining_calls_accurate():
    """forecast()['remaining_calls'] equals remaining_calls() and both decrease on record_call."""
    from hive.core.budgeter import Budgeter
    b = Budgeter(daily_cap=50)
    b.record_call()
    b.record_call()
    forecast = b.forecast()
    assert forecast["remaining_calls"] == b.remaining_calls()
    assert forecast["remaining_calls"] == 48


def test_budgeter_warning_status_none_below_threshold():
    """warning_status() returns None when usage is well below the near-cap threshold."""
    from hive.core.budgeter import Budgeter
    b = Budgeter(daily_cap=1000)
    for _ in range(10):
        b.record_call()
    # 10/1000 = 1% — far below the default 90% warning threshold
    assert b.warning_status() is None


# --- Wave 3T additional tests ---------------------------------------------------

def test_budgeter_is_near_cap_false_below_threshold():
    """is_near_cap() returns False when usage is far below the warn threshold."""
    from hive.core.budgeter import Budgeter
    b = Budgeter(daily_cap=1000)
    b.record_call()
    assert b.is_near_cap() is False


def test_budgeter_is_near_cap_true_above_threshold():
    """is_near_cap() returns True when usage is near the cap."""
    from hive.core.budgeter import Budgeter
    b = Budgeter(daily_cap=10)
    for _ in range(9):  # 90% used
        b.record_call()
    assert b.is_near_cap() is True


def test_budgeter_snapshot_returns_dict():
    """snapshot() returns a dict with at least calls_today key."""
    from hive.core.budgeter import Budgeter
    b = Budgeter(daily_cap=100)
    b.record_call()
    snap = b.snapshot()
    assert isinstance(snap, dict)
    assert "calls_today" in snap


def test_budgeter_reset_daily_resets_calls():
    """reset_daily() brings calls_today back to 0."""
    from hive.core.budgeter import Budgeter
    b = Budgeter(daily_cap=100)
    b.record_call()
    b.record_call()
    b.reset_daily()
    assert b.remaining_calls() == 100


def test_budgeter_calls_per_hour_returns_float():
    """calls_per_hour() returns a non-negative float."""
    from hive.core.budgeter import Budgeter
    b = Budgeter(daily_cap=100)
    b.record_call()
    rate = b.calls_per_hour()
    assert isinstance(rate, float)
    assert rate >= 0.0


def test_eventbus_total_subscribers_increases():
    """total_subscribers() increases after subscribe() calls."""
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    initial = bus.total_subscribers()
    bus.subscribe(EventType.MEMORY_STORE, lambda e: None)
    bus.subscribe(EventType.AGENT_TURN_START, lambda e: None)
    assert bus.total_subscribers() >= initial + 2
