"""Durable observability ledger shared by telemetry and self-mod history.

The ledger is append-only for inference and proposal outcomes. It keeps the
accounting source of truth in the existing state database without making core
depend on observability: callers receive plain aggregate mappings.
"""
from __future__ import annotations

import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable


class ObservabilityLedger:
    """Persist inference telemetry and self-mod proposal outcomes in SQLite."""

    def __init__(self, db_path: str | Path, *, run_id: str | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._path = str(db_path)
        self._clock = clock
        self.run_id = run_id or str(uuid.uuid4())
        self._lock = threading.RLock()
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        # Keep each EventBus retry short; a durable write retries transient
        # contention below instead of silently discarding a completed inference.
        self._db.execute("PRAGMA busy_timeout=250")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS telemetry(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL,
                  ts REAL NOT NULL,
                  day TEXT NOT NULL,
                  model TEXT NOT NULL,
                  input_tokens INTEGER NOT NULL,
                  output_tokens INTEGER NOT NULL,
                  cost_usd REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_telemetry_day ON telemetry(day);
                CREATE TABLE IF NOT EXISTS selfmod_history(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL,
                  ts REAL NOT NULL,
                  title TEXT NOT NULL,
                  dry_run INTEGER NOT NULL,
                  tier TEXT NOT NULL,
                  branch TEXT,
                  pr_url TEXT,
                  outcome TEXT NOT NULL,
                  ok INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_selfmod_history_ts ON selfmod_history(ts DESC);
                """
            )

    @staticmethod
    def _day(ts: float) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(ts))

    @staticmethod
    def _nonnegative_finite(value: object, *, default: float = 0.0) -> float:
        try:
            parsed = float(value or 0.0)
        except (TypeError, ValueError):
            return default
        return parsed if math.isfinite(parsed) and parsed >= 0.0 else default

    @staticmethod
    def _bounded_int(value: object) -> int:
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError, OverflowError):
            return 0
        return max(0, min(parsed, 2**63 - 1))

    def _write(self, operation: Callable[[], int | None]) -> int | None:
        """Retry a transient SQLite lock; never silently drop a ledger record."""
        for attempt in range(6):
            try:
                with self._lock, self._db:
                    return operation()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
        raise AssertionError("unreachable")  # pragma: no cover

    def record_inference(self, data: dict[str, Any]) -> None:
        """Append one completed inference using the already-calculated cost."""
        ts = self._nonnegative_finite(data.get("ts", self._clock()), default=self._clock())

        def insert() -> int:
            self._db.execute(
                """
                INSERT INTO telemetry
                  (run_id, ts, day, model, input_tokens, output_tokens, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.run_id,
                    ts,
                    self._day(ts),
                    str(data.get("model", "?") or "?"),
                    self._bounded_int(data.get("input_tokens", 0)),
                    self._bounded_int(data.get("output_tokens", 0)),
                    self._nonnegative_finite(data.get("cost_usd", 0.0)),
                ),
            )
            return 0
        self._write(insert)

    def telemetry_totals(self, *, day: str | None = None) -> dict[str, Any]:
        """Return a JSON-safe aggregate, optionally restricted to one local day."""
        where = "WHERE day=?" if day else ""
        params: tuple[str, ...] = (day,) if day else ()
        with self._lock:
            row = self._db.execute(
                f"""
                SELECT COUNT(*) AS inference_calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cost_usd), 0.0) AS cost_usd
                FROM telemetry {where}
                """,
                params,
            ).fetchone()
            model_rows = self._db.execute(
                f"""
                SELECT model, COUNT(*) AS calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cost_usd), 0.0) AS cost_usd
                FROM telemetry {where} GROUP BY model
                """,
                params,
            ).fetchall()
        by_model = {str(item["model"]): int(item["calls"]) for item in model_rows}
        cost_by_model = {str(item["model"]): float(item["cost_usd"]) for item in model_rows}
        tokens_by_model = {
            str(item["model"]): {
                "input": int(item["input_tokens"]),
                "output": int(item["output_tokens"]),
            }
            for item in model_rows
        }
        return {
            "inference_calls": int(row["inference_calls"]),
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "cost_usd": float(row["cost_usd"]),
            "by_model": by_model,
            "cost_by_model": cost_by_model,
            "tokens_by_model": tokens_by_model,
        }

    def record_selfmod(self, record: dict[str, Any]) -> int:
        """Append a terminal self-mod proposal record."""
        def insert() -> int:
            cursor = self._db.execute(
                """
                INSERT INTO selfmod_history
                  (run_id, ts, title, dry_run, tier, branch, pr_url, outcome, ok)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.run_id,
                    self._nonnegative_finite(record.get("ts", self._clock()), default=self._clock()),
                    str(record.get("title", "")),
                    int(bool(record.get("dry_run"))),
                    str(record.get("tier", "auto")),
                    record.get("branch"),
                    record.get("pr_url"),
                    str(record.get("outcome", record.get("stage", "unknown"))),
                    int(bool(record.get("ok"))),
                ),
            )
            return int(cursor.lastrowid)
        row_id = self._write(insert)
        assert isinstance(row_id, int)  # narrow _write's generic return for type checkers
        return row_id

    def selfmod_history(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return terminal proposal outcomes newest first."""
        with self._lock:
            rows = self._db.execute(
                """
                SELECT id, ts, title, dry_run, tier, branch, pr_url, outcome, ok
                FROM selfmod_history ORDER BY id DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "_ledger_id": int(row["id"]),
                "ts": float(row["ts"]),
                "title": str(row["title"]),
                "dry_run": bool(row["dry_run"]),
                "tier": str(row["tier"]),
                "branch": row["branch"],
                "pr_url": row["pr_url"],
                "stage": str(row["outcome"]),
                "outcome": str(row["outcome"]),
                "ok": bool(row["ok"]),
            }
            for row in rows
        ]

    def clear_selfmod_history(self) -> int:
        """Clear persisted proposal records and return the deleted count."""
        with self._lock, self._db:
            count = int(self._db.execute("SELECT COUNT(*) FROM selfmod_history").fetchone()[0])
            self._db.execute("DELETE FROM selfmod_history")
        return count

    def close(self) -> None:
        with self._lock:
            self._db.close()
