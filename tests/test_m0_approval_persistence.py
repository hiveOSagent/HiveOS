"""M0 #123 regression tests for recovery of operational safety state."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from starlette.testclient import TestClient

from hive.core.approval import gate
from hive.core.approval_enhancements import ApprovalGateEnhancements, ExpirationPolicy, enhance
from hive.core.config import HiveConfig
from hive.core.safety_state import SafetyStateStore
from hive.gateway.app import create_app
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS
from hive.tools.executor import DispatchStatus, ToolDispatch


def _enhancer(db_path, now: list[float], *, ttl: float = 60.0) -> ApprovalGateEnhancements:
    return ApprovalGateEnhancements(
        gate,
        policy=ExpirationPolicy(ttl_seconds=ttl),
        clock=lambda: now[0],
        state_store=SafetyStateStore(db_path),
    )


def test_pending_approval_rehydrates_after_process_restart(tmp_path):
    now = [1000.0]
    first = _enhancer(tmp_path / "state.sqlite", now)
    approval_id = gate.request("deploy", {"target": "prod"}, "ship", "danger")
    first.audit_request(approval_id)

    gate._pending.clear()  # emulate process death: only SQLite survives
    restarted = _enhancer(tmp_path / "state.sqlite", now)

    assert restarted._requested_at[approval_id] == 1000.0
    assert gate.pending() == [{
        "id": approval_id, "tool": "deploy", "args": {"target": "prod"},
        "reason": "ship", "kind": "danger",
    }]


def test_rehydrate_discards_expired_approval_before_it_can_be_decided(tmp_path):
    now = [1000.0]
    first = _enhancer(tmp_path / "state.sqlite", now, ttl=10.0)
    approval_id = gate.request("deploy", {}, "ship")
    first.audit_request(approval_id)

    gate._pending.clear()
    now[0] = 1011.0
    restarted = _enhancer(tmp_path / "state.sqlite", now, ttl=10.0)

    assert approval_id not in restarted._requested_at
    assert gate.pending() == []
    assert SafetyStateStore(tmp_path / "state.sqlite").consume_approval(approval_id) is None


def test_rehydrated_approval_is_consumed_once_under_concurrent_decisions(tmp_path):
    now = [1000.0]
    first = _enhancer(tmp_path / "state.sqlite", now)
    approval_id = gate.request("deploy", {}, "ship")
    first.audit_request(approval_id)

    gate._pending.clear()
    restarted = _enhancer(tmp_path / "state.sqlite", now)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(
            lambda _: restarted.resolve_with_outcome(approval_id, True),
            range(2),
        ))

    assert sum(item is not None for item, _ in outcomes) == 1
    assert sum(outcome is not None for _, outcome in outcomes) == 1
    assert SafetyStateStore(tmp_path / "state.sqlite").consume_approval(approval_id) is None


class _Router:
    async def complete(self, *args, **kwargs):
        return CompletionResult(text="ok", model="test")

    async def aclose(self):
        return None


def test_runtime_rehydrates_once_and_concurrent_api_decisions_execute_once(tmp_path):
    """The production composition root preserves the approval across restart."""
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    HiveOS.build(cfg, router=_Router())
    approval_id = gate.request("read_file", {"path": str(tmp_path / "x")}, "read")
    enhance.audit_request(approval_id)

    gate._pending.clear()  # process death drops only the in-memory protected gate
    restarted = HiveOS.build(cfg, router=_Router())
    assert any(item["id"] == approval_id for item in gate.pending())

    executions: list[tuple[str, dict]] = []

    async def execute_once(name: str, args: dict) -> ToolDispatch:
        executions.append((name, args))
        return ToolDispatch(DispatchStatus.OK)

    restarted.tool_executor.execute_approved = execute_once

    def decide() -> int:
        with TestClient(create_app(restarted)) as client:
            return client.post(
                "/approvals/decide",
                json={"approval_id": approval_id, "approved": True},
                headers={"X-Hive-Token": "change_me"},
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(lambda _: decide(), range(2)))

    assert statuses == [200, 404]
    assert executions == [("read_file", {"path": str(tmp_path / "x")})]
