"""
approval_enhancements.py — production-grade hardening for the PROTECTED gate.

Core/approval_gate.py is the immutable danger firewall (Kamil rule: NEVER edit).
This module wraps it with the production concerns a long-running autonomous agent
needs:

  - ExpirationPolicy    pending approvals expire after a TTL (default 30 min)
  - KillSwitch          global emergency stop; halts new requests, force-rejects pending
  - AuditHistory        structured, queryable record of every decision
  - BatchResolve        one human decision can cover multiple pending calls
  - Hooks               emit EventBus events on approval_resolved / kill_switch / expired

The PROTECTED gate keeps its job: pattern detection + `pending()` / `request()` /
`resolve()`. This file adds the missing operational hardening (expiry, kill, audit)
on top, by reading & wrapping — never by replacing — the canonical gate.

Design rules:
  - NEVER modify Core/approval_gate.py.
  - NEVER weaken the danger check; only add operational state.
  - Everything is thread-safe at the Python GIL level; explicit locks used where
    a multi-step read-modify-write matters (kill switch engage, expire sweep).
"""
from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable

from hive.core.events import EventBus, EventType
from hive.core.safety_state import SafetyStateStore

log = logging.getLogger("hive.gate.enhancements")


# --------------------------------------------------------------------- enums


class DecisionOutcome(str, enum.Enum):
    """The terminal state a pending approval can reach."""
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    KILLED = "killed"          # rejected because the kill-switch was engaged


# ----------------------------------------------------------------- dataclasses


@dataclass(slots=True)
class AuditRecord:
    """Structured record of one resolved (or terminated) approval.

    Stored in an in-memory ring buffer for /approvals/history. The canonical
    approval still lives in the gate's dict until resolved; this record is the
    post-resolution audit trail (who/when/what/why)."""
    id: str
    tool: str
    args: dict
    reason: str
    kind: str                 # "danger" | "money" | other
    outcome: DecisionOutcome
    decided_at: float
    requested_at: float
    decided_by: str           # "human:<channel>" | "system:expire" | "system:kill"
    note: str = ""            # free-form context (e.g. batch_id, reason string)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["outcome"] = self.outcome.value
        return d


@dataclass(slots=True)
class ExpirationPolicy:
    """How long a pending approval stays alive before automatic rejection."""
    ttl_seconds: float = 1800.0   # 30 minutes default
    enabled: bool = True

    def is_expired(self, requested_at: float, now_ts: float) -> bool:
        if not self.enabled:
            return False
        return (now_ts - requested_at) > self.ttl_seconds


# ----------------------------------------------------------------- core class


class ApprovalGateEnhancements:
    """Operational hardening for the PROTECTED ApprovalGate.

    Reads the gate, never writes to its source. Tracks expiration, kill switch,
    and structured audit history independently. Singleton: `enhance` below.
    """

    def __init__(
        self,
        gate: Any,                                       # Core/approval_gate.ApprovalGate
        *,
        policy: ExpirationPolicy | None = None,
        events: EventBus | None = None,
        history_max: int = 1000,
        clock: Callable[[], float] | None = None,
        state_store: SafetyStateStore | None = None,
    ) -> None:
        self._gate = gate
        self._policy = policy or ExpirationPolicy()
        self._events = events
        self._history: list[AuditRecord] = []
        self._history_max = max(1, int(history_max))
        self._clock = clock or time.time
        self._kill_switch = threading.Event()           # set = active
        self._kill_engaged_by: str | None = None
        self._kill_engaged_at: float | None = None
        self._lock = threading.RLock()                  # protects _history + kill state
        # requested_at: id -> ts, indexed when request_audited() is called.
        self._requested_at: dict[str, float] = {}
        self._state_store = state_store
        if state_store is not None:
            self.rehydrate_pending()

    # ----- public read-only API --------------------------------------------

    def is_killed(self) -> bool:
        """True when the kill-switch is engaged (emergency stop active)."""
        return self._kill_switch.is_set()

    def kill_state(self) -> dict:
        """Snapshot of kill-switch state for /approvals/emergency-stop GET."""
        return {
            "active": self.is_killed(),
            "engaged_by": self._kill_engaged_by,
            "engaged_at": self._kill_engaged_at,
        }

    def policy(self) -> dict:
        return {"ttl_seconds": self._policy.ttl_seconds,
                "enabled": self._policy.enabled}

    def history(self, limit: int = 50, *, tool: str | None = None,
                outcome: DecisionOutcome | str | None = None,
                since: float | None = None) -> list[AuditRecord]:
        """Return the most recent AuditRecords (filtered), newest first."""
        with self._lock:
            items = list(reversed(self._history))
        if tool is not None:
            items = [r for r in items if r.tool == tool]
        if outcome is not None:
            target = outcome.value if isinstance(outcome, DecisionOutcome) else str(outcome)
            items = [r for r in items if r.outcome.value == target]
        if since is not None:
            items = [r for r in items if r.decided_at >= since]
        return items[:max(1, int(limit))]

    def history_stats(self) -> dict:
        """Aggregate counts grouped by outcome and by tool."""
        with self._lock:
            items = list(self._history)
        by_outcome: dict[str, int] = {}
        by_tool: dict[str, int] = {}
        for r in items:
            by_outcome[r.outcome.value] = by_outcome.get(r.outcome.value, 0) + 1
            by_tool[r.tool] = by_tool.get(r.tool, 0) + 1
        return {"total": len(items),
                "by_outcome": by_outcome,
                "by_tool": dict(sorted(by_tool.items(),
                                       key=lambda kv: -kv[1])[:10]),
                "kill_switch": self.kill_state()}

    # ----- request path integration ----------------------------------------

    def audit_request(self, approval_id: str, requested_at: float | None = None) -> None:
        """Record the wall-clock time a pending approval was created.

        The gate only stores `id, tool, args, reason, kind`. We mirror the request
        time here so we can compute expiration later. Called by the gateway on
        every gate.request() — see /approvals/decide wiring.
        """
        ts = self._clock() if requested_at is None else float(requested_at)
        with self._lock:
            self._requested_at[approval_id] = ts
            if self._state_store is not None:
                pending = getattr(self._gate, "_pending", {})
                item = pending.get(approval_id)
                if item is None:
                    raise RuntimeError(f"approval {approval_id} disappeared before persistence")
                self._state_store.record_approval(item, ts)

    def configure_persistence(self, db_path: str | None) -> list[str]:
        """Bind this wrapper to a state DB and rehydrate its live approval queue.

        A None path is intended for isolated tests and detaches the singleton.
        """
        with self._lock:
            self._state_store = None if db_path is None else SafetyStateStore(db_path)
            self._requested_at.clear()
            return self.rehydrate_pending()

    def rehydrate_pending(self) -> list[str]:
        """Restore live rows into the gate and discard stale rows at startup."""
        if self._state_store is None:
            return []
        now = self._clock()
        ttl = self._policy.ttl_seconds if self._policy.enabled else None
        live, expired = self._state_store.load_pending(now=now, ttl_seconds=ttl)
        pending = getattr(self._gate, "_pending", {})
        for item in live:
            approval_id = str(item["id"])
            pending.setdefault(approval_id, {
                "id": approval_id,
                "tool": item["tool"],
                "args": item["args"],
                "reason": item["reason"],
                "kind": item["kind"],
            })
            self._requested_at[approval_id] = float(item["requested_at"])
        return expired

    def is_request_blocked(self) -> bool:
        """True when a new approval request should be refused outright.

        Currently: only the kill-switch blocks new requests."""
        return self.is_killed()

    # ----- resolve path integration ----------------------------------------

    def resolve_with_history(self, approval_id: str, approved: bool,
                             *, decided_by: str = "human",
                             note: str = "") -> dict | None:
        """Resolve an approval and record its terminal outcome.

        This compatibility wrapper preserves the original return contract. Call
        :meth:`resolve_with_outcome` when the caller must distinguish an approval
        from an expiry or kill-switch rejection without re-reading bounded history.
        """
        item, outcome = self.resolve_with_outcome(
            approval_id, approved, decided_by=decided_by, note=note,
        )
        # A compatibility caller cannot mistake an expired/killed/rejected item
        # for authorization merely because it received the original request.
        return item if outcome is DecisionOutcome.APPROVED else None

    def resolve_with_outcome(self, approval_id: str, approved: bool,
                             *, decided_by: str = "human",
                             note: str = "") -> tuple[dict | None, DecisionOutcome | None]:
        """Resolve one request atomically and return its item plus terminal outcome.

        ``None, None`` means the id was already absent. Holding the enhancement
        lock through gate resolution and audit recording prevents a late approval
        from being mistaken for an executable approval after TTL or kill handling.
        """
        with self._lock:
            if self.is_killed():
                item = self._terminate_due_to_kill(
                    approval_id, decided_by=decided_by, note=note)
                return item, DecisionOutcome.KILLED if item is not None else None

            req_at = self._requested_at.get(approval_id)
            if req_at is not None and self._policy.is_expired(req_at, self._clock()):
                item = self._expire(approval_id, decided_by=decided_by, note=note)
                return item, DecisionOutcome.EXPIRED if item is not None else None

            item = self._take_pending(approval_id, approved)
            if item is None:
                return None, None
            outcome = DecisionOutcome.APPROVED if approved else DecisionOutcome.REJECTED
            self._record(AuditRecord(
                id=approval_id,
                tool=str(item.get("tool", "")),
                args=dict(item.get("args") or {}),
                reason=str(item.get("reason", "")),
                kind=str(item.get("kind", "danger")),
                outcome=outcome,
                decided_at=self._clock(),
                requested_at=req_at if req_at is not None else self._clock(),
                decided_by=decided_by,
                note=note,
            ))
            self._emit(EventType.APPROVAL_RESOLVED, approval_id=approval_id,
                       outcome=outcome.value, tool=item.get("tool"))
            return item, outcome

    def resolve_batch(self, ids: Iterable[str], approved: bool,
                      *, decided_by: str = "human",
                      note: str = "") -> list[dict]:
        """Resolve several pending approvals with one human decision.

        Each id is resolved independently — if some are unknown, expired, or
        killed, only the successful ones return a dict. Returns the list of
        resolved items in input order.
        """
        results = []
        for aid in ids:
            item = self.resolve_with_history(aid, approved, decided_by=decided_by,
                                            note=note or "batch")
            if item is not None:
                results.append(item)
        return results

    # ----- emergency stop --------------------------------------------------

    def engage_kill_switch(self, *, engaged_by: str = "operator",
                           note: str = "") -> dict:
        """Engage the global emergency stop.

        After engagement:
          - any resolve_with_history() short-circuits with outcome=KILLED
          - any new approval request should be refused (is_request_blocked)
          - all currently pending approvals are force-terminated with KILLED

        Returns the snapshot of kill state + count of items killed.
        """
        now = self._clock()
        with self._lock:
            already_active = self.is_killed()
            self._kill_switch.set()
            self._kill_engaged_by = engaged_by
            self._kill_engaged_at = now
            # Snapshot the pending ids without mutating gate state here; the
            # next caller of resolve_with_history() will terminate them.
            pending_ids = list(getattr(self._gate, "_pending", {}).keys())

        killed_ids: list[str] = []
        if not already_active:
            for aid in pending_ids:
                if self._terminate_due_to_kill(aid, decided_by=engaged_by,
                                               note=note) is not None:
                    killed_ids.append(aid)
        killed = len(killed_ids)
        log.warning("KILL SWITCH engaged by %s at %s; %s pending killed",
                    engaged_by, now, killed)
        return {"active": True, "engaged_by": engaged_by,
                "engaged_at": now, "pending_killed": killed,
                "killed_ids": killed_ids, "note": note}

    def release_kill_switch(self, *, released_by: str = "operator") -> dict:
        """Disengage the emergency stop. The system returns to normal."""
        with self._lock:
            was_active = self.is_killed()
            self._kill_switch.clear()
            self._kill_engaged_by = None
            self._kill_engaged_at = None
        log.warning("KILL SWITCH released by %s (was_active=%s)",
                    released_by, was_active)
        return {"active": False, "released_by": released_by,
                "was_active": was_active}

    # ----- expiration sweep ------------------------------------------------

    def sweep_expired(self) -> list[str]:
        """Find every currently-pending approval past TTL and expire it.

        Returns the list of expired ids. Idempotent: calling twice in a row
        returns the second call's empty list (the first already removed them).
        """
        if not self._policy.enabled:
            return []
        now = self._clock()
        expired: list[str] = []
        # Snapshot ids + requested_at under lock to avoid concurrent mutation.
        with self._lock:
            candidates = [(aid, ts) for aid, ts in self._requested_at.items()
                          if self._policy.is_expired(ts, now)]
        for aid, _ in candidates:
            if self._expire(aid, decided_by="system:expire",
                            note="ttl reached") is not None:
                expired.append(aid)
        return expired

    # ----- internals -------------------------------------------------------

    def _terminate_due_to_kill(self, aid: str, *, decided_by: str,
                               note: str) -> dict | None:
        """Force-reject a pending approval because the kill-switch is on."""
        item = self._take_pending(aid, False)
        if item is None:
            return None
        req_at = self._requested_at.pop(aid, self._clock())
        self._record(AuditRecord(
            id=aid,
            tool=str(item.get("tool", "")),
            args=dict(item.get("args") or {}),
            reason=str(item.get("reason", "")),
            kind=str(item.get("kind", "danger")),
            outcome=DecisionOutcome.KILLED,
            decided_at=self._clock(),
            requested_at=req_at,
            decided_by=decided_by or "system:kill",
            note=note,
        ))
        self._emit(EventType.APPROVAL_RESOLVED, approval_id=aid,
                   outcome=DecisionOutcome.KILLED.value,
                   tool=item.get("tool"))
        return item

    def _expire(self, aid: str, *, decided_by: str, note: str) -> dict | None:
        """Force-reject a pending approval because TTL elapsed."""
        item = self._take_pending(aid, False)
        if item is None:
            return None
        req_at = self._requested_at.pop(aid, self._clock())
        self._record(AuditRecord(
            id=aid,
            tool=str(item.get("tool", "")),
            args=dict(item.get("args") or {}),
            reason=str(item.get("reason", "")),
            kind=str(item.get("kind", "danger")),
            outcome=DecisionOutcome.EXPIRED,
            decided_at=self._clock(),
            requested_at=req_at,
            decided_by=decided_by,
            note=note,
        ))
        self._emit(EventType.APPROVAL_RESOLVED, approval_id=aid,
                   outcome=DecisionOutcome.EXPIRED.value,
                   tool=item.get("tool"))
        return item

    def _record(self, rec: AuditRecord) -> None:
        with self._lock:
            self._history.append(rec)
            if len(self._history) > self._history_max:
                # Trim oldest; keep newest `history_max`.
                self._history = self._history[-self._history_max:]
            self._requested_at.pop(rec.id, None)
            if self._state_store is not None:
                self._state_store.delete_approval(rec.id)

    def _take_pending(self, approval_id: str, approved: bool) -> dict | None:
        """Remove one pending approval from durable state before gate resolution."""
        if self._state_store is not None:
            stored = self._state_store.consume_approval(approval_id)
            if stored is None:
                return None
        item = self._gate.resolve(approval_id, approved)
        if item is None and self._state_store is not None:
            log.error("persisted approval %s was absent from the gate; refusing it", approval_id)
        return item

    def _emit(self, event_type: EventType, **data: object) -> None:
        if self._events is None:
            return
        try:
            self._events.publish(event_type, dict(data))
        except Exception as exc:  # noqa: BLE001 - observability must never break gate
            log.warning("event emit failed for %s: %s", event_type, exc)

    def clear_history(self) -> int:
        """Discard all recorded audit records. Returns the count cleared."""
        with self._lock:
            n = len(self._history)
            self._history.clear()
        return n


# Module-level singleton bound to the canonical gate.
from hive.core.approval import gate as _canonical_gate  # noqa: E402

enhance = ApprovalGateEnhancements(_canonical_gate)


__all__ = [
    "ApprovalGateEnhancements",
    "AuditRecord",
    "DecisionOutcome",
    "ExpirationPolicy",
    "enhance",
]
