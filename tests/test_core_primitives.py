"""Tests for the P1 core primitives + PROTECTED-file bridges."""
from __future__ import annotations

import dataclasses

import pytest

from hive.core.registry import RegistryBase
from hive.core.events import EventBus, EventType
from hive.core.types import Message, Role, Conversation, ToolCall
from hive.core.config import HiveConfig


def test_registry_isolation_and_ops():
    class A(RegistryBase[str]):
        pass

    class B(RegistryBase[str]):
        pass

    A.register_value("x", "a-value")
    assert A.contains("x") and not B.contains("x")  # isolation
    assert A.get("x") == "a-value"
    with pytest.raises(ValueError):
        A.register_value("x", "dup")  # duplicate detection
    assert list(A.keys()) == ["x"]
    A.clear()
    assert not A.contains("x")


def test_registry_decorator_and_create():
    class Reg(RegistryBase[type]):
        pass

    @Reg.register("thing")
    class Thing:
        def __init__(self, n: int) -> None:
            self.n = n

    obj = Reg.create("thing", 5)
    assert obj.n == 5


def test_eventbus_pubsub_and_isolation():
    bus = EventBus(record_history=True)
    seen: list[str] = []
    bus.subscribe(EventType.TOOL_CALL_START, lambda e: seen.append(e.data.get("tool", "")))

    def boom(_e):
        raise RuntimeError("bad subscriber")

    bus.subscribe(EventType.TOOL_CALL_START, boom)  # must not break others
    bus.publish(EventType.TOOL_CALL_START, {"tool": "read_file"})
    bus.publish(EventType.INFERENCE_START, {})  # different type, ignored by sub

    assert seen == ["read_file"]
    assert len(bus.history()) == 2


def test_eventbus_history_count_and_clear():
    bus = EventBus(record_history=True)
    bus.publish(EventType.TOOL_CALL_START, {})
    bus.publish(EventType.INFERENCE_END, {})
    assert bus.history_count() == 2
    cleared = bus.clear_history()
    assert cleared == 2
    assert bus.history_count() == 0


def test_eventbus_unsubscribe_all():
    bus = EventBus()
    calls = []
    bus.subscribe(EventType.APPROVAL_REQUESTED, lambda e: calls.append(1))
    bus.publish(EventType.APPROVAL_REQUESTED, {})
    assert calls == [1]
    removed = bus.unsubscribe_all(EventType.APPROVAL_REQUESTED)
    assert removed == 1
    bus.publish(EventType.APPROVAL_REQUESTED, {})
    assert calls == [1]  # no new calls after unsubscribe


def test_eventbus_subscribe_once():
    bus = EventBus()
    calls = []
    bus.subscribe_once(EventType.BUDGET_BLOCK, lambda e: calls.append(e.data.get("reason")))
    bus.publish(EventType.BUDGET_BLOCK, {"reason": "cap"})
    bus.publish(EventType.BUDGET_BLOCK, {"reason": "window"})  # second fire must not call
    assert calls == ["cap"]  # fired exactly once


def test_message_to_dict_roundtrip():
    m = Message(role=Role.ASSISTANT, content="hi",
                tool_calls=[ToolCall(id="c1", name="t", arguments="{}")])
    d = m.to_dict()
    assert d["role"] == "assistant"
    assert d["tool_calls"][0]["function"]["name"] == "t"


def test_conversation_sliding_window():
    c = Conversation(max_messages=2)
    for i in range(4):
        c.add(Message(role=Role.USER, content=str(i)))
    assert [m.content for m in c.messages] == ["2", "3"]


def test_tool_result_bool():
    from hive.core.types import ToolResult
    ok = ToolResult(tool_name="t", content="hi", success=True)
    fail = ToolResult(tool_name="t", content="err", success=False)
    assert bool(ok) is True
    assert bool(fail) is False


def test_config_validate_default_secret(tmp_path):
    cfg = HiveConfig(
        root=tmp_path, data_dir=tmp_path, state_db=tmp_path / "s.db",
        exec_provider="minimax", minimax_anthropic_base="x", minimax_openai_base="x",
        minimax_api_key="", anthropic_base="x", anthropic_api_key="",
        exec_model="MiniMax-M3", exec_fallback_model="MiniMax-M2.7", aux_model="MiniMax-M2.7",
        planner_cmd="codex", planner_enabled=False, planner_timeout=120.0,
        remains_url="", daily_call_cap=3000, window_warn_pct=70.0,
        host="0.0.0.0", port=8088, secret="change_me", production_mode=False,
        mnemosyne_mcp_url="", mnemosyne_home=tmp_path, obsidian_vault=tmp_path,
        heartbeat_sec=900, max_concurrent_agents=3,
        github_token="", github_repo="", github_owner="",
        telegram_token="", telegram_webhook_secret="",
        telegram_allowed_user_ids=frozenset(), telegram_allowed_chat_ids=frozenset(),
        sandbox_image="", mcp_servers=(), max_iterations=30, max_per_tool=50,
        selfmod_failure_threshold=3, tool_timeout=60.0,
        shell_provider="local", shell_docker_image="alpine:latest",
        cors_origins="*", max_message_len=32000, ws_idle_timeout=300.0,
        smtp_host="", smtp_port=587, smtp_user="", smtp_pass="", smtp_to="",
        slack_webhook="", discord_webhook="", selfmod_proactive_interval=10,
        slack_bot_token="", slack_signing_secret="",
        discord_bot_token="", discord_public_key="", discord_application_id="",
        smtp_from="", smtp_webhook_secret="",
        slack_allowed_user_ids=frozenset(), discord_allowed_user_ids=frozenset(),
        email_allowed_senders=frozenset(),
        deploy_ssh_host="", deploy_ssh_key="",
        stripe_secret_key="", stripe_customer_id="",
        learning_loop_enabled=False, learning_eval_timeout=60.0,
        selfmod_enable_safety_checks=True, selfmod_safety_max_files=20,
        selfmod_failure_cooldown_sec=1800.0,
        budget_forecast_alert_days=1,
        budget_daily_spend_cap_usd=0.0,
        entity_resolution_enabled=True, entity_resolution_alias_map="",
        heartbeat_proactive_interval_sec=86400,
        heartbeat_stale_fact_days=30,
        heartbeat_stale_commitment_days=7,
    )
    issues = cfg.validate()
    assert any("change_me" in i for i in issues)

def test_config_from_env_without_dotenv_keeps_storage_under_explicit_root(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.data_dir == tmp_path / "data"
    assert cfg.state_db == tmp_path / "data" / "hive.sqlite"
    assert cfg.autonomy_enabled is False
    assert cfg.autonomous_selfmod_enabled is False


def test_config_rejects_selfmod_without_autonomy():
    cfg = HiveConfig(**{**_base_cfg(), "autonomous_selfmod_enabled": True})
    assert "HIVE_AUTONOMOUS_SELFMOD_ENABLED requires HIVE_AUTONOMY_ENABLED=true" in cfg.validate()


def _base_cfg(tmp_path=None):
    from pathlib import Path
    p = tmp_path or Path("/tmp/hive_test_cfg")
    return dict(
        root=p, data_dir=p, state_db=p / "s.db",
        exec_provider="minimax", minimax_anthropic_base="x", minimax_openai_base="x",
        minimax_api_key="key", anthropic_base="x", anthropic_api_key="",
        exec_model="MiniMax-M3", exec_fallback_model="MiniMax-M2.7", aux_model="MiniMax-M2.7",
        planner_cmd="codex", planner_enabled=False, planner_timeout=120.0,
        remains_url="", daily_call_cap=3000, window_warn_pct=70.0,
        host="0.0.0.0", port=8088, secret="s3cr3t", production_mode=False,
        mnemosyne_mcp_url="", mnemosyne_home=p, obsidian_vault=p,
        heartbeat_sec=900, max_concurrent_agents=3,
        github_token="", github_repo="", github_owner="",
        telegram_token="", telegram_webhook_secret="",
        telegram_allowed_user_ids=frozenset(), telegram_allowed_chat_ids=frozenset(),
        sandbox_image="", mcp_servers=(), max_iterations=30, max_per_tool=50,
        selfmod_failure_threshold=3, tool_timeout=60.0,
        shell_provider="local", shell_docker_image="alpine:latest",
        cors_origins="*", max_message_len=32000, ws_idle_timeout=300.0,
        smtp_host="", smtp_port=587, smtp_user="", smtp_pass="", smtp_to="",
        slack_webhook="", discord_webhook="", selfmod_proactive_interval=10,
        slack_bot_token="", slack_signing_secret="",
        discord_bot_token="", discord_public_key="", discord_application_id="",
        smtp_from="", smtp_webhook_secret="",
        slack_allowed_user_ids=frozenset(), discord_allowed_user_ids=frozenset(),
        email_allowed_senders=frozenset(),
        deploy_ssh_host="", deploy_ssh_key="",
        stripe_secret_key="", stripe_customer_id="",
        learning_loop_enabled=False, learning_eval_timeout=60.0,
        selfmod_enable_safety_checks=True, selfmod_safety_max_files=20,
        selfmod_failure_cooldown_sec=1800.0,
        budget_forecast_alert_days=1,
        budget_daily_spend_cap_usd=0.0,
        entity_resolution_enabled=True, entity_resolution_alias_map="",
        heartbeat_proactive_interval_sec=86400,
        heartbeat_stale_fact_days=30,
        heartbeat_stale_commitment_days=7,
    )


def test_config_validate_bad_exec_provider():
    cfg = HiveConfig(
        **{**_base_cfg(), "exec_provider": "unknown_provider"},
    )
    issues = cfg.validate()
    assert any("HIVE_EXEC_PROVIDER" in i for i in issues)


def test_config_validate_bad_shell_provider():
    cfg = HiveConfig(
        **{**_base_cfg(), "shell_provider": "ssh"},
    )
    issues = cfg.validate()
    assert any("HIVE_SHELL_PROVIDER" in i for i in issues)


def test_config_validate_zero_max_iterations():
    cfg = HiveConfig(
        **{**_base_cfg(), "max_iterations": 0},
    )
    issues = cfg.validate()
    assert any("HIVE_MAX_ITERATIONS" in i for i in issues)


def test_config_validate_minimax_no_key():
    cfg = HiveConfig(
        **{**_base_cfg(), "exec_provider": "minimax", "minimax_api_key": ""},
    )
    issues = cfg.validate()
    assert any("MINIMAX_API_KEY" in i for i in issues)


def test_eventbus_subscriber_count():
    bus = EventBus()
    assert bus.subscriber_count(EventType.INFERENCE_END) == 0
    bus.subscribe(EventType.INFERENCE_END, lambda e: None)
    bus.subscribe(EventType.INFERENCE_END, lambda e: None)
    assert bus.subscriber_count(EventType.INFERENCE_END) == 2
    assert bus.subscriber_count(EventType.TOOL_CALL_END) == 0


def test_eventbus_total_subscribers():
    bus = EventBus()
    assert bus.total_subscribers() == 0
    bus.subscribe(EventType.INFERENCE_END, lambda e: None)
    bus.subscribe(EventType.TOOL_CALL_END, lambda e: None)
    bus.subscribe(EventType.TOOL_CALL_END, lambda e: None)
    assert bus.total_subscribers() == 3


def test_eventbus_history_by_type():
    bus = EventBus(record_history=True)
    bus.publish(EventType.INFERENCE_END, {"model": "m"})
    bus.publish(EventType.INFERENCE_END, {})
    bus.publish(EventType.TOOL_CALL_END, {})
    by_type = bus.history_by_type()
    assert by_type.get("inference_end") == 2
    assert by_type.get("tool_call_end") == 1
    assert "budget_block" not in by_type


def test_eventbus_history_by_type_empty():
    bus = EventBus(record_history=True)
    assert bus.history_by_type() == {}


def test_eventbus_recent_events_empty():
    bus = EventBus(record_history=True)
    assert bus.recent_events() == []


def test_eventbus_recent_events_returns_newest_first():
    bus = EventBus(record_history=True)
    bus.publish(EventType.TOOL_CALL_START, {"a": 1})
    bus.publish(EventType.TOOL_CALL_END, {"b": 2})
    events = bus.recent_events(n=10)
    assert len(events) == 2
    assert events[0]["event_type"] == EventType.TOOL_CALL_END.value  # newest first
    assert events[1]["event_type"] == EventType.TOOL_CALL_START.value


def test_eventbus_recent_events_capped_by_n():
    bus = EventBus(record_history=True)
    for _ in range(10):
        bus.publish(EventType.TOOL_CALL_START, {})
    assert len(bus.recent_events(n=3)) == 3


def test_eventbus_recent_events_serialisable_fields():
    bus = EventBus(record_history=True)
    bus.publish(EventType.TOOL_CALL_START, {"tool": "shell", "args": {}})
    ev = bus.recent_events(n=1)[0]
    assert "event_type" in ev and "data" in ev and "ts" in ev


def test_hiveconfig_builds_from_env_and_is_frozen(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVE_EXEC_MODEL", "MiniMax-M9")
    monkeypatch.setenv("HIVE_PORT", "9099")
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "d"))
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.exec_model == "MiniMax-M9"
    assert cfg.port == 9099  # typed: int, not str
    assert cfg.state_db == tmp_path / "d" / "hive.sqlite"
    # frozen: impossible-state protection
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.port = 1  # type: ignore[misc]
    # no import-time side effects: data dir is created only on explicit request
    assert not cfg.data_dir.exists()
    cfg.ensure_dirs()
    assert cfg.data_dir.is_dir()


# --- llm_summary / is_production / to_safe_dict ----------------------------

def test_hiveconfig_llm_summary(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    summary = cfg.llm_summary()
    assert "exec_model" in summary
    assert "exec_provider" in summary
    assert "planner_enabled" in summary
    assert "daily_call_cap" in summary
    assert isinstance(summary["planner_enabled"], bool)


def test_hiveconfig_is_production_false_for_default(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    # Default secret is "change_me" → not production
    assert cfg.is_production() is False


def test_hiveconfig_is_production_true_for_real_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET", "super-secure-secret-abc")
    monkeypatch.setenv("HIVE_HOST", "10.0.0.1")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.is_production() is True


def test_hiveconfig_parses_inbound_sender_allowlists(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_TELEGRAM_ALLOWED_USER_IDS", " 1,2 ,, ")
    monkeypatch.setenv("HIVE_TELEGRAM_ALLOWED_CHAT_IDS", "-100, group")
    monkeypatch.setenv("HIVE_SLACK_ALLOWED_USER_IDS", "U1,U2")
    monkeypatch.setenv("HIVE_DISCORD_ALLOWED_USER_IDS", "D1")
    monkeypatch.setenv("HIVE_EMAIL_ALLOWED_SENDERS", "Kamil@Example.com, other@example.com")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.telegram_allowed_user_ids == frozenset({"1", "2"})
    assert cfg.telegram_allowed_chat_ids == frozenset({"-100", "group"})
    assert cfg.slack_allowed_user_ids == frozenset({"U1", "U2"})
    assert cfg.discord_allowed_user_ids == frozenset({"D1"})
    assert cfg.email_allowed_senders == frozenset({"kamil@example.com", "other@example.com"})


def test_hiveos_build_rejects_default_secret_in_explicit_production_mode(tmp_path, monkeypatch):
    from hive.runtime import HiveOS

    monkeypatch.setenv("HIVE_PRODUCTION", "true")
    monkeypatch.setenv("HIVE_SECRET", "change_me")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    with pytest.raises(RuntimeError, match="HIVE_PRODUCTION=true"):
        HiveOS.build(cfg, router=object())


def test_hiveos_build_rejects_empty_secret_in_explicit_production_mode(tmp_path, monkeypatch):
    from hive.runtime import HiveOS

    monkeypatch.setenv("HIVE_PRODUCTION", "true")
    monkeypatch.setenv("HIVE_SECRET", "")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    with pytest.raises(RuntimeError, match="non-empty HIVE_SECRET"):
        HiveOS.build(cfg, router=object())


@pytest.mark.parametrize(
    ("overrides", "expected_issue"),
    [
        ({"production_mode": True, "secret": ""}, "non-empty HIVE_SECRET"),
        ({"autonomy_enabled": True}, "HIVE_APPROVER_KEY"),
        ({"telegram_token": "token"}, "TELEGRAM_WEBHOOK_SECRET"),
        (
            {"telegram_token": "token", "telegram_webhook_secret": "secret"},
            "HIVE_TELEGRAM_ALLOWED",
        ),
        ({"slack_signing_secret": "secret"}, "HIVE_SLACK_ALLOWED_USER_IDS"),
        ({"discord_public_key": "key"}, "HIVE_DISCORD_ALLOWED_USER_IDS"),
        ({"smtp_webhook_secret": "secret"}, "HIVE_EMAIL_ALLOWED_SENDERS"),
    ],
)
def test_config_validate_reports_inbound_startup_requirements(tmp_path, overrides, expected_issue):
    cfg = HiveConfig(**{**_base_cfg(tmp_path), **overrides})

    assert any(expected_issue in issue for issue in cfg.validate())


def test_hiveconfig_to_safe_dict_redacts_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "secret-key-xyz")
    monkeypatch.setenv("HIVE_GITHUB_TOKEN", "ghp_faketoken")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    safe = cfg.to_safe_dict()
    assert safe["secret"] == "***"
    assert safe["minimax_api_key"] == "***"
    assert safe["github_token"] == "***"
    assert "telegram_allowed_user_ids" not in safe
    assert "telegram_allowed_chat_ids" not in safe
    assert "slack_allowed_user_ids" not in safe
    assert "discord_allowed_user_ids" not in safe
    assert "email_allowed_senders" not in safe
    # Non-secret fields are not redacted
    assert safe["exec_model"] == cfg.exec_model
    assert safe["port"] == cfg.port


def test_hiveconfig_to_safe_dict_empty_secrets_not_redacted(tmp_path):
    # When keys are empty strings, they should show "" not "***"
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    safe = cfg.to_safe_dict()
    # minimax_api_key is "" by default — should be "" not "***"
    assert safe["minimax_api_key"] == ""


def test_config_validate_zero_max_message_len():
    cfg = HiveConfig(**{**_base_cfg(), "max_message_len": 0})
    issues = cfg.validate()
    assert any("HIVE_MAX_MESSAGE_LEN" in i for i in issues)


def test_config_validate_zero_ws_idle_timeout():
    cfg = HiveConfig(**{**_base_cfg(), "ws_idle_timeout": 0.0})
    issues = cfg.validate()
    assert any("HIVE_WS_IDLE_TIMEOUT" in i for i in issues)


# --- New tests: Message equality, tool_calls, ToolResult error, Registry, Role ---

def test_message_equality():
    """Two Messages with the same role and content compare equal."""
    m1 = Message(role=Role.USER, content="hello")
    m2 = Message(role=Role.USER, content="hello")
    assert m1 == m2


def test_message_with_tool_calls():
    """A Message with tool_calls retains them after construction."""
    tc = ToolCall(id="tc1", name="search", arguments='{"q": "test"}')
    m = Message(role=Role.ASSISTANT, content="", tool_calls=[tc])
    assert len(m.tool_calls) == 1
    assert m.tool_calls[0].name == "search"
    assert m.tool_calls[0].id == "tc1"


def test_tool_result_error_flag():
    """ToolResult with success=False is falsy and not successful."""
    from hive.core.types import ToolResult
    tr = ToolResult(tool_name="t", content="failed", success=False)
    assert bool(tr) is False
    assert tr.success is False


def test_registry_base_list_all():
    """register 3 items, values() returns all 3."""
    class Reg3(RegistryBase[str]):
        pass

    Reg3.register_value("a", "alpha")
    Reg3.register_value("b", "beta")
    Reg3.register_value("c", "gamma")
    all_values = Reg3.values()
    assert sorted(all_values) == ["alpha", "beta", "gamma"]
    Reg3.clear()


def test_registry_base_clear():
    """register items then clear(), values() returns empty list."""
    class Reg4(RegistryBase[str]):
        pass

    Reg4.register_value("x", "ex")
    Reg4.register_value("y", "why")
    assert Reg4.count() == 2
    Reg4.clear()
    assert Reg4.values() == []
    assert Reg4.count() == 0


def test_role_all_members_present():
    """Role enum has USER, ASSISTANT, SYSTEM, and TOOL members."""
    member_names = {r.name for r in Role}
    assert "USER" in member_names
    assert "ASSISTANT" in member_names
    assert "SYSTEM" in member_names
    assert "TOOL" in member_names


# --- Additional core primitives tests ----------------------------------------

def test_message_default_content_empty():
    """Message default content is empty string."""
    from hive.core.types import Message, Role
    m = Message(role=Role.USER)
    assert m.content == ""


def test_message_tool_call_id_stored():
    """Message with tool_call_id preserves it."""
    from hive.core.types import Message, Role
    m = Message(role=Role.TOOL, content="result", tool_call_id="tc1")
    assert m.tool_call_id == "tc1"


def test_tool_call_fields():
    """ToolCall stores id, name, arguments."""
    from hive.core.types import ToolCall
    tc = ToolCall(id="call1", name="search", arguments='{"q": "test"}')
    assert tc.id == "call1" and tc.name == "search" and "test" in tc.arguments


def test_tool_result_success_default_true():
    """ToolResult.success defaults to True."""
    from hive.core.types import ToolResult
    r = ToolResult(tool_name="t", content="ok")
    assert r.success is True


def test_tool_result_cost_default_zero():
    """ToolResult.cost_usd defaults to 0.0."""
    from hive.core.types import ToolResult
    r = ToolResult(tool_name="t", content="ok")
    assert r.cost_usd == 0.0


def test_tool_result_failure_is_not_success():
    """ToolResult(success=False) is flagged as failure."""
    from hive.core.types import ToolResult
    r = ToolResult(tool_name="t", content="error", success=False)
    assert r.success is False


def test_role_system_member():
    """Role has SYSTEM member."""
    from hive.core.types import Role
    assert hasattr(Role, "SYSTEM")


def test_role_tool_member():
    """Role has TOOL member."""
    from hive.core.types import Role
    assert hasattr(Role, "TOOL")


def test_message_metadata_default_empty():
    """Message metadata defaults to empty dict."""
    from hive.core.types import Message, Role
    m = Message(role=Role.USER, content="hi")
    assert m.metadata == {}


# ---------------------------------------------------------------------------
# Six new tests appended for coverage expansion
# ---------------------------------------------------------------------------

def test_eventbus_publish_no_subscribers_does_not_raise():
    """Publishing to an event type with zero subscribers must not raise."""
    bus = EventBus()
    # No subscribers registered — must complete silently
    bus.publish(EventType.SELFMOD_START, {"reason": "test"})


def test_eventbus_history_max_bounds_rolling_window():
    """When history_max=3, only the last 3 events are retained after 5 publishes."""
    bus = EventBus(record_history=True, history_max=3)
    for i in range(5):
        bus.publish(EventType.TOOL_CALL_START, {"i": i})
    assert bus.history_count() == 3


def test_registry_get_missing_key_raises():
    """get() on a key that was never registered must raise KeyError."""
    class RegMissing(RegistryBase[str]):
        pass

    with pytest.raises(KeyError):
        RegMissing.get("does_not_exist")


def test_registry_items_reflects_registered_entries():
    """items() returns key-value pairs matching what was registered."""
    class RegItems(RegistryBase[int]):
        pass

    RegItems.register_value("one", 1)
    RegItems.register_value("two", 2)
    result = dict(RegItems.items())
    assert result == {"one": 1, "two": 2}
    RegItems.clear()


def test_conversation_max_messages_none_keeps_all():
    """With max_messages=None, all messages are retained without sliding."""
    c = Conversation(max_messages=None)
    for i in range(10):
        c.add(Message(role=Role.USER, content=str(i)))
    assert len(c.messages) == 10


def test_hiveconfig_llm_summary_contains_expected_keys(tmp_path):
    """llm_summary() must contain exec_model, exec_provider, planner_enabled, daily_call_cap."""
    from hive.core.config import HiveConfig
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    summary = cfg.llm_summary()
    for key in ("exec_model", "exec_provider", "planner_enabled", "daily_call_cap"):
        assert key in summary, f"Key {key!r} missing from llm_summary()"


# --- Wave 3S additional tests ---------------------------------------------------

def test_event_bus_recent_events_returns_list():
    """recent_events() returns a list when record_history=True."""
    bus = EventBus(record_history=True)
    bus.publish(EventType.TOOL_CALL_START, {"x": 1})
    events = bus.recent_events(5)
    assert isinstance(events, list)
    assert len(events) >= 1


def test_event_bus_history_by_type_filters():
    """history_by_type() returns a dict with counts per event type."""
    bus = EventBus(record_history=True)
    bus.publish(EventType.TOOL_CALL_START, {"a": 1})
    bus.publish(EventType.MEMORY_STORE, {"b": 2})
    counts = bus.history_by_type()
    assert isinstance(counts, dict)
    assert counts.get("tool_call_start", 0) >= 1


def test_event_bus_clear_history_resets_count():
    """clear_history() resets history_count() to 0 (requires record_history=True)."""
    bus = EventBus(record_history=True)
    bus.publish(EventType.TOOL_CALL_START, {})
    assert bus.history_count() >= 1
    bus.clear_history()
    assert bus.history_count() == 0


def test_conversation_add_increments_length():
    """Each add() call increments the messages list by 1."""
    c = Conversation()
    assert len(c.messages) == 0
    c.add(Message(role=Role.USER, content="hello"))
    assert len(c.messages) == 1
    c.add(Message(role=Role.ASSISTANT, content="hi"))
    assert len(c.messages) == 2


def test_conversation_messages_store_roles():
    """Messages stored via add() preserve the role."""
    c = Conversation()
    c.add(Message(role=Role.USER, content="q"))
    c.add(Message(role=Role.ASSISTANT, content="a"))
    assert c.messages[0].role == Role.USER
    assert c.messages[1].role == Role.ASSISTANT


def test_event_bus_subscriber_count_increases_with_subscribe():
    """subscriber_count() increases by 1 after each subscribe() call."""
    bus = EventBus()
    initial = bus.subscriber_count(EventType.TOOL_CALL_START)
    bus.subscribe(EventType.TOOL_CALL_START, lambda e: None)
    assert bus.subscriber_count(EventType.TOOL_CALL_START) == initial + 1


# --- Wave 4H additional tests ---------------------------------------------------

def test_wave4h_eventbus_agent_turn_events_recorded():
    """Publishing AGENT_TURN_START and AGENT_TURN_END both appear in history."""
    bus = EventBus(record_history=True)
    bus.publish(EventType.AGENT_TURN_START, {"turn": 1})
    bus.publish(EventType.AGENT_TURN_END, {"turn": 1})
    by_type = bus.history_by_type()
    assert by_type.get("agent_turn_start") == 1
    assert by_type.get("agent_turn_end") == 1


def test_wave4h_eventbus_agent_tick_events_recorded():
    """Publishing AGENT_TICK_START and AGENT_TICK_END both appear in history_by_type."""
    bus = EventBus(record_history=True)
    bus.publish(EventType.AGENT_TICK_START, {"tick": 0})
    bus.publish(EventType.AGENT_TICK_END, {"tick": 0})
    by_type = bus.history_by_type()
    assert by_type.get("agent_tick_start") == 1
    assert by_type.get("agent_tick_end") == 1


def test_wave4h_eventbus_memory_retrieve_event():
    """MEMORY_RETRIEVE appears in history_by_type after publish."""
    bus = EventBus(record_history=True)
    bus.publish(EventType.MEMORY_RETRIEVE, {"query": "test"})
    by_type = bus.history_by_type()
    assert by_type.get("memory_retrieve") == 1


def test_wave4h_eventbus_telemetry_record_event():
    """TELEMETRY_RECORD can be published and subscribed without error."""
    bus = EventBus(record_history=True)
    received = []
    bus.subscribe(EventType.TELEMETRY_RECORD, lambda e: received.append(e.data))
    bus.publish(EventType.TELEMETRY_RECORD, {"metric": "latency_ms", "value": 42})
    assert received == [{"metric": "latency_ms", "value": 42}]
    assert bus.history_by_type().get("telemetry_record") == 1


def test_wave4h_eventbus_approval_resolved_event():
    """APPROVAL_RESOLVED can be published; recent_events includes it."""
    bus = EventBus(record_history=True)
    bus.publish(EventType.APPROVAL_RESOLVED, {"approved": True})
    events = bus.recent_events(n=1)
    assert len(events) == 1
    assert events[0]["event_type"] == "approval_resolved"


def test_wave4h_eventbus_selfmod_end_event():
    """SELFMOD_END can be published and appears in history."""
    bus = EventBus(record_history=True)
    bus.publish(EventType.SELFMOD_END, {"result": "ok"})
    by_type = bus.history_by_type()
    assert by_type.get("selfmod_end") == 1


def test_wave4h_registry_create_with_kwargs():
    """create() passes keyword arguments to the registered class constructor."""
    class RegKwargs(RegistryBase[type]):
        pass

    @RegKwargs.register("item")
    class Item:
        def __init__(self, color: str = "red", size: int = 1) -> None:
            self.color = color
            self.size = size

    obj = RegKwargs.create("item", color="blue", size=7)
    assert obj.color == "blue"
    assert obj.size == 7
    RegKwargs.clear()


def test_wave4h_eventbus_unsubscribe_all_multiple_types():
    """unsubscribe_all() only removes subscribers for the specified type."""
    bus = EventBus()
    tick_calls = []
    turn_calls = []
    bus.subscribe(EventType.AGENT_TICK_END, lambda e: tick_calls.append(1))
    bus.subscribe(EventType.AGENT_TICK_END, lambda e: tick_calls.append(2))
    bus.subscribe(EventType.AGENT_TURN_END, lambda e: turn_calls.append(1))
    removed = bus.unsubscribe_all(EventType.AGENT_TICK_END)
    assert removed == 2
    assert bus.subscriber_count(EventType.AGENT_TICK_END) == 0
    assert bus.subscriber_count(EventType.AGENT_TURN_END) == 1


# --- Wave 4L new tests -------------------------------------------------------

def test_wave4l_eventbus_publish_no_subscribers_silent():
    """publish() with zero subscribers for that type completes without raising."""
    bus = EventBus()
    bus.publish(EventType.BUDGET_BLOCK, {"amount": 99})


def test_wave4l_eventbus_publish_returns_none():
    """publish() returns None (no explicit return value)."""
    bus = EventBus()
    result = bus.publish(EventType.INFERENCE_START, {})
    assert result is None


def test_wave4l_registry_get_unknown_key_raises():
    """get() on a key never registered raises KeyError."""
    class RegWave4L(RegistryBase[str]):
        pass

    with pytest.raises(KeyError):
        RegWave4L.get("never_registered_key")


def test_wave4l_eventtype_values_are_strings():
    """Every EventType enum member has a str value."""
    for member in EventType:
        assert isinstance(member.value, str)


def test_wave4l_subscriber_count_zero_after_unsubscribe_all():
    """subscriber_count() returns 0 after unsubscribe_all() for that type."""
    bus = EventBus()
    bus.subscribe(EventType.MEMORY_STORE, lambda e: None)
    bus.subscribe(EventType.MEMORY_STORE, lambda e: None)
    assert bus.subscriber_count(EventType.MEMORY_STORE) == 2
    bus.unsubscribe_all(EventType.MEMORY_STORE)
    assert bus.subscriber_count(EventType.MEMORY_STORE) == 0


def test_wave4l_record_history_false_no_accumulation():
    """With record_history=False (default), history_count() stays 0 after publishes."""
    bus = EventBus()
    bus.publish(EventType.TOOL_CALL_START, {})
    bus.publish(EventType.TOOL_CALL_END, {})
    assert bus.history_count() == 0


def test_wave4l_multiple_subscribers_all_receive_event():
    """Multiple subscribers on the same event type each receive the published event."""
    bus = EventBus()
    received_a: list[str] = []
    received_b: list[str] = []
    received_c: list[str] = []
    bus.subscribe(EventType.TOOL_CALL_START, lambda e: received_a.append(e.data.get("tool", "")))
    bus.subscribe(EventType.TOOL_CALL_START, lambda e: received_b.append(e.data.get("tool", "")))
    bus.subscribe(EventType.TOOL_CALL_START, lambda e: received_c.append(e.data.get("tool", "")))
    bus.publish(EventType.TOOL_CALL_START, {"tool": "shell"})
    assert received_a == ["shell"]
    assert received_b == ["shell"]
    assert received_c == ["shell"]


def test_wave4l_eventbus_record_history_true_accumulates():
    """With record_history=True, history_count() grows with each publish."""
    bus = EventBus(record_history=True)
    assert bus.history_count() == 0
    bus.publish(EventType.INFERENCE_END, {})
    assert bus.history_count() == 1
    bus.publish(EventType.INFERENCE_END, {})
    assert bus.history_count() == 2


# --- Phase 2: soft LoopGuard + prefix-cache fix --------------------------------

def test_soft_loop_guard_pivot_turn():
    """When LoopGuard trips, orchestrator makes a final pivot call without tools."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from hive.agents.orchestrator import ConversationOrchestrator
    from hive.core.types import ToolResult

    pivot_called_without_tools = []

    async def fake_complete(messages, *, system="", tools=None, **kw):
        result = MagicMock()
        if tools is None:
            pivot_called_without_tools.append(True)
            result.text = "I see I was looping — let me wrap up."
        else:
            # Always return a tool call to trigger the loop guard quickly
            tc = MagicMock()
            tc.name = "shell"
            tc.arguments = '{"cmd":"echo hi"}'
            tc.id = "c1"
            result.text = ""
            result.tool_calls = [tc]
        return result

    router = MagicMock()
    router.complete = fake_complete

    # Executor that always OK-returns
    async def fake_dispatch(name, args, reason=""):
        d = MagicMock()
        d.status.name = "OK"
        r = ToolResult(tool_name=name, content="ok")
        d.result = r
        from hive.tools.executor import DispatchStatus
        d.status = DispatchStatus.OK
        return d

    executor = MagicMock()
    executor.execute = fake_dispatch

    orch = ConversationOrchestrator(router, tool_executor=executor,
                                    tools={"shell": MagicMock()}, max_per_tool=1)
    result = asyncio.run(orch.ask("do something"))
    assert pivot_called_without_tools, "pivot turn (tools=None) was not called"
    assert "loop" in result.content.lower() or "wrap" in result.content.lower()


def test_prefix_cache_stable_with_channel_hint():
    """restore_or_build_system_prompt bakes channel_hint into stored prompt."""
    from hive.context.prompt_builder import restore_or_build_system_prompt

    stored: dict[str, str] = {}

    class _Store:
        def get_system_prompt(self, sid): return stored.get(sid)
        def save_system_prompt(self, sid, text): stored[sid] = text

    store = _Store()
    # First call — builds and saves with hint baked in
    p1 = restore_or_build_system_prompt(store, "s1", "mem", channel_hint="telegram")
    assert "telegram" in p1.lower() or "Active surface" in p1
    # Second call — must restore byte-exact (NOT rebuild)
    p2 = restore_or_build_system_prompt(store, "s1", "mem", channel_hint="telegram")
    assert p1 == p2, "Second call returned different string — cache miss"
    # Third call with no hint for different session — different prompt
    p3 = restore_or_build_system_prompt(store, "s2", "mem", channel_hint="")
    assert p3 != p1


# ── Phase 3: self-mod quality ────────────────────────────────────────────────

def test_parse_test_output_extracts_failed_names():
    """_parse_test_output surfaces FAILED lines and short summary."""
    from hive.runtime import HiveOS
    raw = (
        "collecting ... done\n"
        "FAILED tests/test_foo.py::test_bar - AssertionError: 0 != 1\n"
        "FAILED tests/test_foo.py::test_baz - ValueError: bad\n"
        "short test summary info\n"
        "FAILED tests/test_foo.py::test_bar\n"
        "2 failed in 0.5s\n"
    )
    result = HiveOS._parse_test_output(raw)
    assert "test_bar" in result
    assert "test_baz" in result
    assert len(result) <= 2000


def test_parse_test_output_fallback_on_no_failed_lines():
    """_parse_test_output returns tail of raw when no FAILED lines found."""
    from hive.runtime import HiveOS
    raw = "some output\n" + "x" * 2000
    result = HiveOS._parse_test_output(raw)
    assert len(result) <= 2000
    assert result  # not empty


def test_build_symptom_includes_task_failures(tmp_path):
    """_build_symptom_context includes recent task failures in output."""
    import asyncio
    from unittest.mock import MagicMock, AsyncMock
    from hive.autonomy.tasks import TaskBoard
    from hive.runtime import HiveOS

    board = TaskBoard(tmp_path / "t.db")
    tid = board.enqueue("tool", {"tool": "shell", "reason": "test"})
    board.claim(tid)
    board.fail(tid, "connection refused")

    hive = MagicMock(spec=HiveOS)
    hive.audit_log = MagicMock()
    hive.audit_log.error_rate.return_value = 0.01  # below threshold → skip tool errors
    hive.task_board = board
    hive.self_modifier = MagicMock()
    hive.self_modifier.failed_proposals.return_value = []
    # Bind unbound method
    hive._build_symptom_context = HiveOS._build_symptom_context.__get__(hive, HiveOS)

    result = asyncio.run(hive._build_symptom_context("base symptom"))
    assert "connection refused" in result
    assert "base symptom" in result


def test_build_symptom_skips_tool_errors_at_low_rate(tmp_path):
    """_build_symptom_context omits tool error section when error_rate < 5%."""
    import asyncio
    from unittest.mock import MagicMock
    from hive.autonomy.tasks import TaskBoard
    from hive.runtime import HiveOS

    board = TaskBoard(tmp_path / "t.db")
    hive = MagicMock(spec=HiveOS)
    hive.audit_log = MagicMock()
    hive.audit_log.error_rate.return_value = 0.02
    hive.task_board = board
    hive.self_modifier = MagicMock()
    hive.self_modifier.failed_proposals.return_value = []
    hive._build_symptom_context = HiveOS._build_symptom_context.__get__(hive, HiveOS)

    result = asyncio.run(hive._build_symptom_context("my symptom"))
    assert "Tool errors" not in result
    assert "my symptom" in result
