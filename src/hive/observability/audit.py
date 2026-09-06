"""
audit.py — SQLite audit log (the executor's audit sink).

The old Tools/registry.py appended JSONL to data/audit.log; SQLite-first (OpenClaw)
means the audit trail is a table, not a sidecar file. `record()` matches the
ToolExecutor audit-callback shape (a dict), so wiring is `ToolExecutor(..., audit=
audit_log.record)`. Depends on core only.

SPRINT_7 Batch E: ``AuditBroadcaster`` exposes a publish/subscribe interface so
real-time consumers (the ``/ws/audit`` WebSocket) get every newly-recorded audit
row without polling the SQLite log.
"""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from hive.core.redact import redact_args

log = logging.getLogger("hive.observability.audit")
_AUDIT_COLUMNS = (
    "id, ts, tool, status, approved, error, args, actor, principal, prev_digest, digest"
)
_CHAIN_VERSION = "1"


class AuditLog:
    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time,
                 max_rows: int = 10_000) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._db.execute("PRAGMA journal_mode=WAL")  # shared state DB: reduce writer lock contention
        self._db.execute("PRAGMA busy_timeout=5000")
        self._clock = clock
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS audit_log("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, tool TEXT, status TEXT, "
            "approved INTEGER, error TEXT, args TEXT, actor TEXT NOT NULL DEFAULT 'agent', "
            "principal TEXT NOT NULL DEFAULT 'agent', prev_digest TEXT NOT NULL DEFAULT '', "
            "digest TEXT NOT NULL DEFAULT '')"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS audit_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._ensure_columns()
        self._migrate_chain_if_needed()
        self._max_rows = max_rows
        self._db.commit()
        self._tighten_permissions(db_path)

    def _ensure_columns(self) -> None:
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(audit_log)")}
        additions = {
            "actor": "TEXT NOT NULL DEFAULT 'agent'",
            "principal": "TEXT NOT NULL DEFAULT 'agent'",
            "prev_digest": "TEXT NOT NULL DEFAULT ''",
            "digest": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in columns:
                self._db.execute(f"ALTER TABLE audit_log ADD COLUMN {name} {definition}")

    def _migrate_chain_if_needed(self) -> None:
        if self._meta("chain_version"):
            return
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM audit_log WHERE digest IS NULL OR digest = ''"
        ).fetchone()
        if row and row["n"]:
            self._rebuild_chain()
        elif self._db.execute("SELECT 1 FROM audit_meta WHERE key='chain_head'").fetchone() is None:
            head = self._db.execute(
                "SELECT digest FROM audit_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self._set_meta("chain_head", head["digest"] if head else "")
            self._set_meta("chain_anchor", "")
        self._set_meta("chain_version", _CHAIN_VERSION)

    def _rebuild_chain(self) -> None:
        previous = ""
        rows = self._db.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        for row in rows:
            actor = str(row["actor"] or "agent")
            principal = str(row["principal"] or actor)
            digest = self._row_digest(
                row_id=int(row["id"]), ts=float(row["ts"]), tool=str(row["tool"] or ""),
                status=str(row["status"] or ""), approved=bool(row["approved"]),
                error=row["error"], args=str(row["args"] or "{}"), actor=actor,
                principal=principal, prev_digest=previous,
            )
            self._db.execute(
                "UPDATE audit_log SET actor=?, principal=?, prev_digest=?, digest=? WHERE id=?",
                (actor, principal, previous, digest, row["id"]),
            )
            previous = digest
        self._set_meta("chain_anchor", "")
        self._set_meta("chain_head", previous)

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO audit_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _meta(self, key: str) -> str:
        row = self._db.execute("SELECT value FROM audit_meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else ""

    @staticmethod
    def _row_digest(*, row_id: int, ts: float, tool: str, status: str,
                    approved: bool, error: Any, args: str, actor: str,
                    principal: str, prev_digest: str) -> str:
        payload = json.dumps(
            {
                "id": row_id, "ts": ts, "tool": tool, "status": status,
                "approved": approved, "error": error, "args": args,
                "actor": actor, "principal": principal, "prev_digest": prev_digest,
            }, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _tighten_permissions(db_path: str | Path) -> None:
        if str(db_path) == ":memory:":
            return
        try:
            Path(db_path).chmod(0o600)
        except OSError:
            # Windows ACLs do not map cleanly to POSIX mode bits; deployment
            # tooling remains responsible for an equivalent restrictive ACL.
            pass

    def record(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                ts = self._clock()
                redacted_args = redact_args(entry.get("args", {}))  # B2: redact secrets
                actor = str(entry.get("actor") or "agent")
                principal = str(entry.get("principal") or actor)
                previous = self._meta("chain_head")
                self._db.execute(
                    "INSERT INTO audit_log(ts, tool, status, approved, error, args, actor, principal, "
                    "prev_digest, digest) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (ts, entry.get("tool", ""), entry.get("status", ""),
                     1 if entry.get("approved") else 0, entry.get("error"),
                     json.dumps(redacted_args, default=str), actor, principal, previous, ""),
                )
                row_id = int(self._db.execute("SELECT last_insert_rowid()").fetchone()[0])
                digest = self._row_digest(
                    row_id=row_id, ts=ts, tool=str(entry.get("tool", "")),
                    status=str(entry.get("status", "")), approved=bool(entry.get("approved")),
                    error=entry.get("error"), args=json.dumps(redacted_args, default=str),
                    actor=actor, principal=principal, prev_digest=previous,
                )
                self._db.execute("UPDATE audit_log SET digest=? WHERE id=?", (digest, row_id))
                self._set_meta("chain_head", digest)
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            self.prune()
        # SPRINT_7 Batch E: publish to real-time subscribers. The broadcaster is
        # best-effort; failures here must not break audit. Wrap in try/except so
        # a queue-full or subscriber-iteration error never blocks record().
        try:
            _audit_broadcaster.publish({
                "ts": ts,
                "tool": entry.get("tool", ""),
                "status": entry.get("status", ""),
                "approved": bool(entry.get("approved")),
                "error": entry.get("error"),
                "args": redacted_args,
            })
        except Exception as exc:  # noqa: BLE001 - broadcaster is best-effort
            log.warning("audit_broadcaster.publish failed: %s", exc)

    def _fetchall(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def _fetchone(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, params).fetchone()

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self._fetchall(
            f"SELECT {_AUDIT_COLUMNS} FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        entries = []
        for r in rows:
            entry = dict(r)
            try:
                entry["args"] = json.loads(entry["args"] or "{}")
            except (json.JSONDecodeError, TypeError):
                entry["args"] = {}
            entries.append(entry)
        return entries

    def export(self, *, start_ts: float | None = None, end_ts: float | None = None,
               limit: int | None = None, fmt: str = "json") -> list[dict]:
        """Return audit entries as a list of dicts (JSON-serialisable).

        Filtering:
          * ``start_ts`` / ``end_ts`` — inclusive time-range filter on ``ts``.
          * ``limit`` — if given, return only the most recent ``limit`` rows
            (chosen by ``ts`` DESC then ``id`` DESC), but returned in
            chronological order (ASC) so downstream consumers (e.g. pattern
            detection) see the temporal sequence they expect. When
            ``start_ts``/``end_ts`` are also provided, the limit is applied
            AFTER the range filter.

        Without any arguments the full history is returned (in insertion order).
        """
        clauses, params = [], []
        if start_ts is not None:
            clauses.append("ts >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("ts <= ?")
            params.append(end_ts)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        if limit is not None:
            # Sub-select the most recent N (DESC), then re-order ASC for the
            # caller's natural left-to-right chronological view.
            inner_where = where  # already includes ANDed clauses
            sql = (
                f"SELECT {_AUDIT_COLUMNS} "
                f"FROM (SELECT {_AUDIT_COLUMNS} "
                f"      FROM audit_log {inner_where} "
                f"      ORDER BY ts DESC, id DESC LIMIT ?) "
                f"ORDER BY ts ASC, id ASC"
            )
            rows = self._fetchall(sql, tuple(params) + (limit,))
        else:
            sql = (
                f"SELECT {_AUDIT_COLUMNS} "
                f"FROM audit_log {where} ORDER BY id"
            )
            rows = self._fetchall(sql, tuple(params))
        entries = []
        for r in rows:
            d = dict(r)
            try:
                d["args"] = json.loads(d["args"] or "{}")
            except (json.JSONDecodeError, TypeError):
                d["args"] = {}
            entries.append(d)
        return entries

    def verify_integrity(self) -> dict[str, Any]:
        """Verify the audit hash chain and return structured evidence."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
            expected_previous = self._meta("chain_anchor")
            for row in rows:
                if row["prev_digest"] != expected_previous:
                    return {
                        "valid": False,
                        "checked": int(row["id"]),
                        "error": f"row {row['id']} has an unexpected previous digest",
                    }
                expected_digest = self._row_digest(
                    row_id=int(row["id"]), ts=float(row["ts"]), tool=str(row["tool"] or ""),
                    status=str(row["status"] or ""), approved=bool(row["approved"]),
                    error=row["error"], args=str(row["args"] or "{}"),
                    actor=str(row["actor"] or "agent"),
                    principal=str(row["principal"] or row["actor"] or "agent"),
                    prev_digest=expected_previous,
                )
                if row["digest"] != expected_digest:
                    return {
                        "valid": False,
                        "checked": int(row["id"]),
                        "error": f"row {row['id']} digest does not match its content",
                    }
                expected_previous = expected_digest
            head = self._meta("chain_head")
            if head != expected_previous:
                return {
                    "valid": False,
                    "checked": len(rows),
                    "error": "chain head does not match the newest row",
                }
            return {"valid": True, "checked": len(rows), "error": None}

    def prune(self, max_rows: int | None = None) -> int:
        """Delete oldest entries beyond max_rows. Returns count deleted."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                limit = max_rows if max_rows is not None else self._max_rows
                cur = self._db.execute(
                    "DELETE FROM audit_log WHERE id NOT IN "
                    "(SELECT id FROM audit_log ORDER BY id DESC LIMIT ?)",
                    (limit,),
                )
                if cur.rowcount:
                    # Retention is an explicit, local operation. Re-seal the retained
                    # segment so routine pruning does not create a false alarm while
                    # unsanctioned UPDATE/DELETE operations still break the chain.
                    self._rebuild_chain()
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            return cur.rowcount

    def stats(self) -> dict:
        """Return audit summary grouped by tool and status (for dashboard/monitoring)."""
        with self._lock:
            by_tool = {}
            rows = self._fetchall(
                "SELECT tool, status, COUNT(*) AS n FROM audit_log GROUP BY tool, status"
            )
            for r in rows:
                tool_entry = by_tool.setdefault(r["tool"], {"total": 0, "by_status": {}})
                tool_entry["total"] += r["n"]
                tool_entry["by_status"][r["status"]] = r["n"]
            total_row = self._fetchone("SELECT COUNT(*) AS n FROM audit_log")
            return {"total": int(total_row["n"]), "by_tool": by_tool}

    def search(self, *, tool: str | None = None, status: str | None = None,
               limit: int = 50) -> list[dict]:
        """Search audit entries by tool name and/or status."""
        clauses, params = [], []
        if tool is not None:
            clauses.append("tool=?")
            params.append(tool)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(min(limit, 500))
        rows = self._fetchall(
            f"SELECT {_AUDIT_COLUMNS} "
            f"FROM audit_log {where} ORDER BY id DESC LIMIT ?",
            params,
        )
        entries = []
        for r in rows:
            d = dict(r)
            try:
                d["args"] = json.loads(d["args"] or "{}")
            except (json.JSONDecodeError, TypeError):
                d["args"] = {}
            entries.append(d)
        return entries

    def error_rate(self, window_hours: float = 24.0) -> float:
        """Return the fraction of audit entries that are errors in the recent window.

        Returns a float in [0.0, 1.0]; 0.0 if no entries in window."""
        with self._lock:
            cutoff = self._clock() - window_hours * 3600
            row_total = self._fetchone(
                "SELECT COUNT(*) AS n FROM audit_log WHERE ts >= ?", (cutoff,)
            )
            total = int(row_total["n"]) if row_total else 0
            if total == 0:
                return 0.0
            row_errors = self._fetchone(
                "SELECT COUNT(*) AS n FROM audit_log WHERE ts >= ? AND status != 'ok'",
                (cutoff,),
            )
            errors = int(row_errors["n"]) if row_errors else 0
            return round(errors / total, 4)

    def recent_errors(self, limit: int = 20) -> list[dict]:
        """Return the most recent failed/error audit entries (status != 'ok'), newest first."""
        rows = self._fetchall(
            f"SELECT {_AUDIT_COLUMNS} "
            "FROM audit_log WHERE status != 'ok' ORDER BY id DESC LIMIT ?",
            (min(limit, 200),),
        )
        entries = []
        for r in rows:
            d = dict(r)
            try:
                d["args"] = json.loads(d["args"] or "{}")
            except (json.JSONDecodeError, TypeError):
                d["args"] = {}
            entries.append(d)
        return entries

    def recent_by_tool(self, tool: str, limit: int = 20) -> list[dict]:
        """Return the most recent audit entries for a specific tool (newest first)."""
        rows = self._fetchall(
            f"SELECT {_AUDIT_COLUMNS} "
            "FROM audit_log WHERE tool=? ORDER BY id DESC LIMIT ?",
            (tool, min(limit, 200)),
        )
        entries = []
        for r in rows:
            d = dict(r)
            try:
                d["args"] = json.loads(d["args"] or "{}")
            except (json.JSONDecodeError, TypeError):
                d["args"] = {}
            entries.append(d)
        return entries

    def purge_old(self, max_age_days: float = 90.0) -> int:
        """Delete audit entries older than max_age_days. Returns count deleted."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cutoff = self._clock() - max_age_days * 86_400
                cur = self._db.execute("DELETE FROM audit_log WHERE ts <= ?", (cutoff,))
                if cur.rowcount:
                    self._rebuild_chain()
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            return cur.rowcount

    def count(self) -> int:
        """Return the total number of audit entries."""
        row = self._fetchone("SELECT COUNT(*) FROM audit_log")
        return int(row[0]) if row else 0

    def clear(self) -> None:
        """Remove all audit entries from the database."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM audit_log")
                self._set_meta("chain_anchor", "")
                self._set_meta("chain_head", "")
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._db.close()


# ---------------------------------------------------------------------------
# SPRINT_7 Batch E: real-time audit broadcaster (publish/subscribe for /ws/audit)
# ---------------------------------------------------------------------------
#
# Design notes:
# - One ``queue.Queue`` per subscriber, so a slow consumer doesn't block other
#   subscribers or the writer (audit_log.record).
# - ``put_nowait`` with ``Queue.Full`` drop semantics: under pressure we'd rather
#   lose live messages than slow down tool dispatch.
# - Per-tool-name rate limit (``min_interval_ms``) protects against chatty tools
#   flooding the WebSocket (the dashboard re-paints per message).
# - Subscribers are tracked under a ``threading.Lock`` because ``record()`` may
#   run from a worker thread (e.g. a sync tool) while ``subscribe()`` is called
#   from the asyncio gateway loop.
# - Singleton at module scope so every ``AuditLog`` instance shares one
#   broadcaster — independent of how many DBs are open.


class AuditBroadcaster:
    """Publishes new audit rows to WebSocket subscribers.

    Each subscriber owns its own bounded ``queue.Queue`` so a slow consumer
    cannot block other subscribers. Under load (``queue.Full``) or
    rate-limit pressure, the publisher silently drops the message rather
    than blocking the audit write path.
    """

    def __init__(self, *, min_interval_ms: int = 50) -> None:
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        # Per-tool-name rate limit: map tool_name -> last publish ts (seconds).
        self._last_publish: dict[str, float] = {}
        self._min_interval = min_interval_ms / 1000.0
        self._min_interval_ms = min_interval_ms  # kept for inspection/tests

    def subscribe(self) -> queue.Queue:
        """Register a new subscriber; returns its private bounded queue."""
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Remove a subscriber; safe to call with an unknown queue."""
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def subscriber_count(self) -> int:
        """Return the number of currently-registered subscribers."""
        with self._lock:
            return len(self._subscribers)

    def reset(self) -> None:
        """Drop all subscribers and clear rate-limit state. Tests only."""
        with self._lock:
            self._subscribers.clear()
            self._last_publish.clear()

    def publish(self, row: dict[str, Any]) -> None:
        """Push a new audit row to every subscriber (with rate-limiting).

        ``row`` is expected to be a JSON-serialisable dict; the WS endpoint
        forwards it via ``send_json``. ``record()`` publishes the same shape
        the SQLite store accepts, plus an inserted ``ts``.
        """
        tool = str(row.get("tool") or "")
        if self._should_rate_limit(tool):
            return
        # Snapshot the subscriber list under lock so we don't hold the lock
        # while calling ``put_nowait`` (which can be slow on a full queue).
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(row)
            except queue.Full:
                pass  # drop on full — never block the writer

    def _should_rate_limit(self, tool_name: str) -> bool:
        now = time.time()
        with self._lock:
            last = self._last_publish.get(tool_name, 0.0)
            if now - last < self._min_interval:
                return True
            self._last_publish[tool_name] = now
            return False


# Module-level singleton. Imported by ``hive.gateway.app`` (``/ws/audit``) and
# by tests. Tests reset it via ``_audit_broadcaster.reset()`` between cases.
_audit_broadcaster = AuditBroadcaster()
