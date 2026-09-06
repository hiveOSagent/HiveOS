"""Signed, durable Telegram approval callbacks kept outside the agent config."""
from __future__ import annotations

import base64
import hmac
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from hive.core.redact import register_secret_values

SIGNING_KEY_ENV = "HIVE_TELEGRAM_APPROVAL_SIGNING_KEY"
_PREFIX = "ta1"
_APPROVE = "a"
_REJECT = "r"
_DEFAULT_TTL_SECONDS = 1800.0


@dataclass(frozen=True, slots=True)
class TelegramApprovalDecision:
    """A verified, single-use Telegram decision bound to one approval."""

    approval_id: str
    approved: bool


class TelegramApprovalVerifier:
    """Issue and atomically consume signed Telegram callback tokens.

    The signing key is deliberately not part of :class:`HiveConfig`: it is read
    once from the process environment and removed before the agent is assembled.
    SQLite keeps a token's expiry and consumed state durable across restarts.
    """

    def __init__(self, signing_key: str, db_path: str | Path, *,
                 ttl_seconds: float = _DEFAULT_TTL_SECONDS,
                 clock=time.time) -> None:
        if not signing_key:
            raise ValueError("Telegram approval signing key must not be empty")
        self._key = signing_key.encode("utf-8")
        self._path = str(db_path)
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.RLock()
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @classmethod
    def from_environment(cls, db_path: str | Path) -> "TelegramApprovalVerifier | None":
        """Consume the gateway-only signing key without leaving it in agent env."""
        signing_key = os.environ.pop(SIGNING_KEY_ENV, "")
        if not signing_key:
            return None
        register_secret_values([signing_key])
        return cls(signing_key, db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_approval_callbacks(
                  token_id TEXT PRIMARY KEY,
                  approval_id TEXT NOT NULL,
                  action TEXT NOT NULL CHECK(action IN ('a', 'r')),
                  expires_at REAL NOT NULL,
                  consumed_at REAL
                )
                """
            )

    def issue(self, approval_id: str) -> tuple[str, str]:
        """Return compact approve/reject callback data for one pending approval."""
        return (
            self._issue(approval_id, _APPROVE),
            self._issue(approval_id, _REJECT),
        )

    def _issue(self, approval_id: str, action: str) -> str:
        token_id = secrets.token_urlsafe(9)
        expires_at = self._clock() + self._ttl_seconds
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM telegram_approval_callbacks WHERE expires_at<?",
                (self._clock(),),
            )
            conn.execute(
                """
                INSERT INTO telegram_approval_callbacks(token_id, approval_id, action, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (token_id, approval_id, action, expires_at),
            )
        signature = self._signature(token_id, approval_id, action, expires_at)
        return f"{_PREFIX}.{token_id}.{action}.{signature}"

    def consume(self, callback_data: str) -> TelegramApprovalDecision | None:
        """Verify and permanently consume a callback, failing closed on every error."""
        parts = callback_data.split(".")
        if len(parts) != 4 or parts[0] != _PREFIX or parts[2] not in {_APPROVE, _REJECT}:
            return None
        _, token_id, action, supplied_signature = parts
        if not token_id or len(callback_data.encode("utf-8")) > 64:
            return None
        now = self._clock()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT approval_id, action, expires_at, consumed_at "
                    "FROM telegram_approval_callbacks WHERE token_id=?",
                    (token_id,),
                ).fetchone()
                if row is None or row["action"] != action or row["consumed_at"] is not None:
                    conn.rollback()
                    return None
                approval_id = str(row["approval_id"])
                expires_at = float(row["expires_at"])
                expected = self._signature(token_id, approval_id, action, expires_at)
                if now > expires_at or not hmac.compare_digest(supplied_signature, expected):
                    conn.rollback()
                    return None
                updated = conn.execute(
                    """
                    UPDATE telegram_approval_callbacks SET consumed_at=?
                    WHERE token_id=? AND consumed_at IS NULL AND expires_at>=?
                    """,
                    (now, token_id, now),
                ).rowcount
                if updated != 1:
                    conn.rollback()
                    return None
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return TelegramApprovalDecision(approval_id=approval_id, approved=action == _APPROVE)

    def _signature(self, token_id: str, approval_id: str, action: str, expires_at: float) -> str:
        payload = f"{_PREFIX}|{token_id}|{approval_id}|{action}|{expires_at:.6f}".encode("utf-8")
        digest = hmac.new(self._key, payload, sha256).digest()[:16]
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
