"""Durable operational safety state kept outside protected core policy."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class SafetyStateStore:
    """SQLite persistence for pending approvals and autonomous-action cooldowns.

    Each operation uses a short-lived connection. This makes restart recovery
    explicit and lets SQLite serialize competing approval decisions safely.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._lock = threading.RLock()
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS approvals_pending(
                  id TEXT PRIMARY KEY,
                  tool TEXT NOT NULL,
                  args_json TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  requested_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS autonomy_cooldowns(
                  name TEXT PRIMARY KEY,
                  last_triggered_at REAL NOT NULL
                );
                """
            )

    def record_approval(self, item: dict[str, Any], requested_at: float) -> None:
        """Persist a newly-created gate item without extending an existing TTL."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO approvals_pending
                  (id, tool, args_json, reason, kind, requested_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(item["id"]),
                    str(item.get("tool", "")),
                    json.dumps(dict(item.get("args") or {}), sort_keys=True),
                    str(item.get("reason", "")),
                    str(item.get("kind", "danger")),
                    float(requested_at),
                ),
            )

    def load_pending(self, *, now: float, ttl_seconds: float | None) -> tuple[list[dict], list[str]]:
        """Atomically return live rows and discard rows already past their TTL."""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute("SELECT * FROM approvals_pending").fetchall()
                expired = [
                    str(row["id"]) for row in rows
                    if ttl_seconds is not None and (now - float(row["requested_at"])) > ttl_seconds
                ]
                if expired:
                    conn.executemany(
                        "DELETE FROM approvals_pending WHERE id=?",
                        [(approval_id,) for approval_id in expired],
                    )
                live = [self._approval_row(row) for row in rows if str(row["id"]) not in expired]
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return live, expired

    def consume_approval(self, approval_id: str) -> dict | None:
        """Atomically take one pending approval so only one decider can execute it."""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM approvals_pending WHERE id=?", (approval_id,)
                ).fetchone()
                if row is None:
                    conn.commit()
                    return None
                conn.execute("DELETE FROM approvals_pending WHERE id=?", (approval_id,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._approval_row(row)

    def delete_approval(self, approval_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM approvals_pending WHERE id=?", (approval_id,))

    def get_cooldown(self, name: str) -> float | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT last_triggered_at FROM autonomy_cooldowns WHERE name=?", (name,)
            ).fetchone()
        return None if row is None else float(row["last_triggered_at"])

    def set_cooldown(self, name: str, last_triggered_at: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO autonomy_cooldowns(name, last_triggered_at) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET last_triggered_at=excluded.last_triggered_at
                """,
                (name, float(last_triggered_at)),
            )

    @staticmethod
    def _approval_row(row: sqlite3.Row) -> dict:
        return {
            "id": str(row["id"]),
            "tool": str(row["tool"]),
            "args": json.loads(str(row["args_json"])),
            "reason": str(row["reason"]),
            "kind": str(row["kind"]),
            "requested_at": float(row["requested_at"]),
        }
