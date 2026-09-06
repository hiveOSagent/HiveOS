"""M1 #127 — durable telemetry and self-modification history regressions."""
from __future__ import annotations

import asyncio
import math
import sqlite3
import threading
import time

from hive.core.budgeter import Budgeter
from hive.core.config import HiveConfig
from hive.core.events import EventBus, EventType
from hive.core.self_mod import SelfModifier
from hive.observability.persistence import ObservabilityLedger
from hive.observability.telemetry import Telemetry
from hive.runtime import HiveOS


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def _runner():
    async def run(cmd, cwd=None):
        command = " ".join(cmd) if isinstance(cmd, list) else cmd
        if command.startswith("git rev-parse"):
            return 0, "deadbeef\n"
        if command.startswith("git diff --name-only"):
            return 0, "src/hive/llm/pricing.py\n"
        if command.startswith("git ls-files --others"):
            return 0, ""
        return 0, "ok"
    return run


async def _apply_ok(_worktree: str) -> list[str]:
    return ["src/hive/llm/pricing.py"]


class _Router:
    async def aclose(self) -> None:
        pass


def test_telemetry_ledger_persists_and_aggregates_local_day(tmp_path):
    db = tmp_path / "hive.sqlite"
    first = ObservabilityLedger(db, run_id="run-a")
    first.record_inference({"model": "MiniMax-M3", "input_tokens": 12,
                            "output_tokens": 8, "cost_usd": 0.25})
    first.close()

    second = ObservabilityLedger(db, run_id="run-b")
    totals = second.telemetry_totals(day=_today())
    assert totals["inference_calls"] == 1
    assert totals["input_tokens"] == 12
    assert totals["output_tokens"] == 8
    assert totals["cost_usd"] == 0.25
    assert totals["tokens_by_model"]["MiniMax-M3"] == {"input": 12, "output": 8}
    second.close()


def test_telemetry_and_budgeter_rehydrate_after_restart(tmp_path):
    db = tmp_path / "hive.sqlite"
    ledger = ObservabilityLedger(db)
    bus = EventBus()
    telemetry = Telemetry(ledger=ledger).attach(bus)
    bus.publish(EventType.INFERENCE_END, {"model": "MiniMax-M3", "input_tokens": 9,
                                          "output_tokens": 3, "cost_usd": 0.15})
    assert telemetry.snapshot()["cost_usd"] == 0.15
    ledger.close()

    restored_ledger = ObservabilityLedger(db)
    restored = Telemetry(ledger=restored_ledger)
    usage = restored_ledger.telemetry_totals(day=_today())
    budgeter = Budgeter(initial_usage=usage)
    assert restored.snapshot()["cost_usd"] == 0.15
    assert restored.snapshot()["inference_calls"] == 1
    assert budgeter.snapshot()["cost_today_usd"] == 0.15
    assert budgeter.snapshot()["calls_today"] == 1
    restored_ledger.close()


def test_runtime_build_rehydrates_current_day_budget_after_restart(tmp_path):
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    first = HiveOS.build(config, router=_Router())
    first.events.publish(EventType.INFERENCE_END, {"model": "MiniMax-M3", "input_tokens": 7,
                                                   "output_tokens": 2, "cost_usd": 0.12})
    asyncio.run(first.aclose())

    restored = HiveOS.build(config, router=_Router())
    assert restored.budgeter.snapshot()["calls_today"] == 1
    assert restored.budgeter.snapshot()["cost_today_usd"] == 0.12
    asyncio.run(restored.aclose())


def test_nonfinite_cost_is_not_persisted_or_hydrated(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "hive.sqlite")
    ledger.record_inference({"model": "MiniMax-M3", "input_tokens": 1,
                             "output_tokens": 1, "cost_usd": math.inf})
    usage = ledger.telemetry_totals(day=_today())
    budgeter = Budgeter(initial_usage={**usage, "cost_usd": math.nan})
    assert usage["cost_usd"] == 0.0
    assert budgeter.snapshot()["cost_today_usd"] == 0.0
    ledger.close()


def test_telemetry_write_retries_a_temporary_sqlite_lock(tmp_path):
    db = tmp_path / "hive.sqlite"
    ledger = ObservabilityLedger(db)
    blocker = sqlite3.connect(db, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")

    def release_lock() -> None:
        blocker.commit()
        blocker.close()

    timer = threading.Timer(0.35, release_lock)
    timer.start()
    try:
        ledger.record_inference({"model": "MiniMax-M3", "input_tokens": 1,
                                 "output_tokens": 1, "cost_usd": 0.01})
    finally:
        timer.join()
    assert ledger.telemetry_totals(day=_today())["inference_calls"] == 1
    ledger.close()


def test_selfmod_history_survives_restart_and_keeps_review_metadata(tmp_path):
    db = tmp_path / "hive.sqlite"
    ledger = ObservabilityLedger(db)
    modifier = SelfModifier(repo_root=str(tmp_path), run=_runner(), history_store=ledger)
    result = asyncio.run(modifier.propose_approved("durable outcome", "description", _apply_ok,
                                                   dry_run=True))
    assert result["ok"] is True
    ledger.close()

    restored_ledger = ObservabilityLedger(db)
    restored = SelfModifier(repo_root=str(tmp_path), run=_runner(), history_store=restored_ledger)
    history = restored.history()
    assert history[0]["title"] == "durable outcome"
    assert history[0]["tier"] == "review"
    assert history[0]["outcome"] == "dry_run"
    restored_ledger.close()


def test_identical_selfmod_outcomes_remain_distinct_in_durable_history(tmp_path, monkeypatch):
    ledger = ObservabilityLedger(tmp_path / "hive.sqlite")
    modifier = SelfModifier(repo_root=str(tmp_path), run=_runner(), history_store=ledger)
    monkeypatch.setattr("hive.core.self_mod.time.time", lambda: 123.0)
    asyncio.run(modifier.propose("same", "description", _apply_ok, dry_run=True))
    asyncio.run(modifier.propose("same", "description", _apply_ok, dry_run=True))
    assert len(modifier.history()) == 2
    ledger.close()
