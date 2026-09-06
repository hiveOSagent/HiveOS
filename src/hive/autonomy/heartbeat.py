"""
heartbeat.py — the never-idle autonomy loop (KEEP from Core/orchestrator.py).

Each tick (M3): fire due cron jobs + commitments onto the durable TaskBoard; if the
board has no due work, plan the next 1-3 tasks from goals + memory and enqueue them
too; then claim and dispatch the due tasks through the gate-routed ToolExecutor
(bounded concurrency), marking each done/failed on the board; finally run sleep-time
memory consolidation (keeper) + skill-lifecycle curation and refresh the budget.

The board is SQLite-backed, so queued work survives a restart and is drained on the
next tick. Subagents are leaves — dispatch executes tools, it does not spawn nested
heartbeats. Drives an assembled HiveOS; `tick()` is one cycle, `run()` is the 24/7 loop.

SPRINT_7 Batch C adds a periodic *proactive* scan (configurable interval) that
surfaces: (a) repeated tool sequences not yet learned as skills, (b) stale facts
in Mnemosyne (if available), and (c) overdue commitments. Each finding becomes
a ``proactive_suggestion`` task row on the board, with priority below human tasks.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from hive.core.events import EventType
from hive.core.safety_state import SafetyStateStore
from hive.runtime import HiveOS
from hive.tools.executor import DispatchStatus

log = logging.getLogger("hive.autonomy.heartbeat")

_DEFAULT_GOALS = (
    "Keep projects moving and surface blockers.",
    "Continuously find gaps and improve HiveOS.",
)

# Finding types emitted by ``proactive_scan``.
TYPE_LEARNED_SKILL_CANDIDATE = "learned_skill_candidate"
TYPE_STALE_FACT = "stale_fact"
TYPE_STALE_COMMITMENT = "stale_commitment"

ALL_FINDING_TYPES = (
    TYPE_LEARNED_SKILL_CANDIDATE, TYPE_STALE_FACT, TYPE_STALE_COMMITMENT,
)

# Priorities — these are user-facing labels carried inside the ProactiveFinding;
# the actual ``TaskBoard`` enqueue uses ``source="proactive_suggestion"`` (sorting
# is by id ASC, so priority ordering is conveyed by enqueue order under that
# source tag, plus the ``priority`` field on the finding itself).
PRIORITY_LOW = "low"
PRIORITY_MEDIUM = "medium"
PRIORITY_HIGH = "high"

ALL_PRIORITIES = (PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH)


@dataclass(slots=True)
class ProactiveFinding:
    """A single observation from the proactive scan.

    ``type`` names the sub-scan that produced it (``learned_skill_candidate``,
    ``stale_fact``, ``stale_commitment``). ``data`` is a JSON-safe dict whose
    schema depends on ``type`` (see ``proactive_scan`` for per-type keys).
    ``priority`` is a hint for downstream consumers; ``created_at`` is set on
    construction.
    """
    type: str
    data: dict = field(default_factory=dict)
    priority: str = PRIORITY_MEDIUM
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.type not in ALL_FINDING_TYPES:
            raise ValueError(
                f"ProactiveFinding.type={self.type!r} must be one of {ALL_FINDING_TYPES}")
        if self.priority not in ALL_PRIORITIES:
            raise ValueError(
                f"ProactiveFinding.priority={self.priority!r} must be one of {ALL_PRIORITIES}")


def _interval_ticks(cfg) -> int:
    """Convert ``heartbeat_proactive_interval_sec`` (seconds) into a tick count.

    ``cfg.heartbeat_sec`` is the tick period in seconds. We round UP so the scan
    runs *at least* every ``interval_sec`` seconds. ``interval_sec <= 0`` (or
    tick period <= 0) returns 0 — the heartbeat treats that as "disabled".
    """
    interval_sec = max(0, int(getattr(cfg, "heartbeat_proactive_interval_sec", 0) or 0))
    tick_period = max(1, int(getattr(cfg, "heartbeat_sec", 1) or 1))
    if interval_sec <= 0:
        return 0
    # round up
    return (interval_sec + tick_period - 1) // tick_period


class Heartbeat:
    def __init__(self, hive: HiveOS, *, goals: list[str] | None = None) -> None:
        self._hive = hive
        self._goals = list(goals or _DEFAULT_GOALS)
        self._sem = asyncio.Semaphore(max(1, hive.config.max_concurrent_agents))
        self._running = False
        self._tick_count = 0
        self._last_proactive_ts: float = float("-inf")  # ensures first run always fires
        # Cooldown between failure-triggered self-mod attempts (loop prevention).
        # Without this, persistent failures re-fire the LLM diagnoser on every tick.
        db_path = getattr(hive.config, "state_db", None)
        self._safety_state = (
            SafetyStateStore(db_path) if isinstance(db_path, (str, Path)) else None
        )
        stored_cooldown = (
            self._safety_state.get_cooldown("failure_self_mod")
            if self._safety_state is not None else None
        )
        self._last_failure_self_mod_ts = (
            float("-inf") if stored_cooldown is None else stored_cooldown
        )
        # Lazy-initialized on first budget-alert tick (avoids constructing a
        # TelegramChannel when Telegram isn't configured).
        self._budget_alert = None
        # Proactive scan counter (BATCH C). Reset modulo
        # ``_interval_ticks()`` so it fires every Nth tick (and is no-op when
        # interval is disabled).
        self._ticks_since_proactive: int = 0
        self._last_proactive_scan_tick: int = -1

    def enqueue(self, task: dict) -> int:
        """Durably enqueue a task (survives restart). Returns the task id."""
        return self._hive.task_board.enqueue("tool", task, source="manual")

    async def tick(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        self._hive.events.publish(EventType.AGENT_TICK_START, {"ts": now})
        result = await self._tick_inner(now)
        self._hive.events.publish(EventType.AGENT_TICK_END, {"ts": time.time(),
                                                               **result})
        return result

    async def _tick_inner(self, now: float) -> dict:
        if not self._hive.config.autonomy_enabled:
            log.info("heartbeat: autonomy disabled by HIVE_AUTONOMY_ENABLED")
            return {"cron": 0, "commitments": 0, "planned": 0, "dispatched": 0,
                    "consolidated": 0, "curated": 0, "self_improved": 0,
                    "proactive_diagnosed": 0, "disabled": True}

        # 1. Scan for stale commitments before due_and_enqueue() marks them
        # fulfilled. Otherwise every genuinely overdue commitment is reset by
        # the scheduler before Scan C sees it, making that scan permanently
        # silent in production.
        proactive_enqueued = 0
        proactive_runs = 0
        try:
            tick_n = _interval_ticks(self._hive.config)
            if tick_n > 0:
                self._ticks_since_proactive += 1
                if self._ticks_since_proactive >= tick_n:
                    self._ticks_since_proactive = 0
                    self._last_proactive_scan_tick = self._tick_count + 1
                    findings = self.proactive_scan()
                    proactive_enqueued = self._emit_proactive_findings(findings, now)
                    proactive_runs = 1
        except Exception as exc:  # noqa: BLE001 - proactive scan must not abort tick
            log.warning("heartbeat: proactive_scan failed: %s", exc)

        # 2. Schedulers populate the durable board.
        cron_fired = self._hive.cron.due_and_enqueue(now)
        commitments_fired = self._hive.commitments.due_and_enqueue(now)

        # 3. If nothing is due, plan fresh work and enqueue it onto the board.
        # proactive_suggestion rows are for-a-human findings (surfaced via
        # GET /tasks), not executable tool tasks — exclude them here so they
        # neither starve the planner (by permanently keeping `due` non-empty)
        # nor get silently claimed+completed by the generic dispatcher below.
        due = [t for t in self._hive.task_board.due(now)
              if t.kind != "proactive_suggestion"]
        planned = 0
        if not due:
            context = self._hive.memory.prefetch("recent tasks goals progress") or "fresh start"
            plan = await self._hive.planner.plan(self._goals, context)
            for task in plan:
                self._hive.task_board.enqueue("tool", task, source="planner")
            planned = len(plan)
            due = [t for t in self._hive.task_board.due(now)
                  if t.kind != "proactive_suggestion"]

        # 4. Claim + dispatch the due tasks; record outcome on the board.
        dispatched = await self._dispatch(due)
        try:
            consolidated = await self._hive.consolidate()
        except Exception as exc:  # noqa: BLE001
            log.warning("heartbeat: consolidation failed: %s", exc)
            consolidated = 0
        curation = self._hive.curate()  # deterministic skill lifecycle (safe, no-op early)
        try:
            await self._hive.curate_umbrellas()
        except Exception as exc:  # noqa: BLE001
            log.warning("heartbeat: curator umbrellas failed: %s", exc)
        try:
            await self._refresh_budget()
        except Exception as exc:  # noqa: BLE001
            log.warning("heartbeat: budget refresh failed: %s", exc)
        # SPRINT_7 Batch F: Telegram alert on forecast status transition.
        # Skipped when no telegram is configured; lazy-instantiates the channel
        # once per process and reuses it.
        try:
            await self._check_budget_alert()
        except Exception as exc:  # noqa: BLE001 - alerting must not break the tick
            log.warning("heartbeat: budget alert check failed: %s", exc)
        curated = len(curation.get("transitions", []))
        # 5. After dispatch: check for repeated failures and trigger self-improvement.
        #    Only fire when ≥threshold recent failures AND the cooldown has elapsed
        #    since the last attempt. Without the cooldown, persistent failures would
        #    re-fire the LLM diagnoser on every single tick.
        #    When the learning loop is enabled (config.learning_loop_enabled=true)
        #    we route through the eval-gated loop instead of the legacy flow
        #    (SPRINT_6 P-F).
        self_improved = 0
        try:
            threshold = self._hive.config.selfmod_failure_threshold
            cooldown = max(0.0, getattr(self._hive.config, "selfmod_failure_cooldown_sec", 1800.0))
            failed = self._hive.task_board.recent_failures(limit=10)
            if (self._hive.config.autonomous_selfmod_enabled and len(failed) >= threshold
                    and (now - self._last_failure_self_mod_ts) >= cooldown):
                symptom = ("Repeated task failures in last tick: "
                           + "; ".join(t.last_error or "unknown" for t in failed[:5]))
                use_learning = bool(
                    getattr(self._hive.config, "learning_loop_enabled", False)
                )
                # Record before invoking the diagnoser. If the process is killed
                # during that call, the next process still suppresses a repeat.
                self._last_failure_self_mod_ts = now
                if self._safety_state is not None:
                    self._safety_state.set_cooldown("failure_self_mod", now)
                outcomes = await self._hive.self_improve_from_symptom(
                    symptom,
                    use_learning_loop=use_learning,
                )
                self_improved = len(outcomes)
        except Exception as exc:  # noqa: BLE001 - self-improve failure must not abort tick
            log.warning("heartbeat: self-improve check failed: %s", exc)

        # 6. Proactive self-diagnose: run every N ticks, but throttle idle runs.
        self._tick_count += 1
        proactive_diagnosed = 0
        interval = getattr(self._hive.config, "selfmod_proactive_interval", 10)
        _PROACTIVE_COOLDOWN = 1800  # 30 min between zero-outcome runs
        if (self._hive.config.autonomous_selfmod_enabled and interval > 0
                and self._tick_count % interval == 0):
            elapsed = now - self._last_proactive_ts
            if elapsed >= _PROACTIVE_COOLDOWN:
                try:
                    log.info("heartbeat: proactive self-diagnose (tick %d)", self._tick_count)
                    result = await self._hive.self_diagnose()
                    proactive_diagnosed = len(result.get("improvement_outcomes", []))
                    log.info("heartbeat: proactive self-diagnose: %d outcome(s)", proactive_diagnosed)
                    self._last_proactive_ts = now
                except Exception as exc:  # noqa: BLE001 - proactive diagnose must not abort tick
                    log.warning("heartbeat: proactive self-diagnose failed: %s", exc)
            else:
                log.info("heartbeat: proactive self-diagnose skipped (cooldown %.0fs remaining)",
                         _PROACTIVE_COOLDOWN - elapsed)

        log.info("heartbeat: cron=%d commitments=%d planned=%d dispatched=%d "
                 "consolidated=%d curated=%d self_improved=%d proactive_diagnosed=%d "
                 "proactive_enqueued=%d proactive_runs=%d",
                 cron_fired, commitments_fired, planned, dispatched, consolidated,
                 curated, self_improved, proactive_diagnosed, proactive_enqueued,
                 proactive_runs)
        return {"cron": cron_fired, "commitments": commitments_fired, "planned": planned,
                "dispatched": dispatched, "consolidated": consolidated, "curated": curated,
                "self_improved": self_improved, "proactive_diagnosed": proactive_diagnosed,
                "proactive_enqueued": proactive_enqueued, "proactive_runs": proactive_runs}

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    def _mnemosyne_facts(self) -> list[dict] | None:
        """Return facts from the active memory provider, or None if unavailable.

        Distinguishes "no Mnemosyne wired" (None) from "Mnemosyne present but
        returned []" (empty list). Mnemosyne's HiveOS adapter exposes its
        knowledge via ``most_important_facts`` on the ABC; the local provider
        implements it. Returns a list of dicts with at least ``created_ts``
        (epoch seconds) and ideally ``last_accessed``; if neither is present
        the caller falls back to ``created_ts``.
        """
        mem = getattr(self._hive, "memory", None)
        if mem is None:
            return None
        if not hasattr(mem, "most_important_facts"):
            return None
        try:
            rows = mem.most_important_facts(limit=500) or []
        except Exception as exc:  # noqa: BLE001 - defensive; never break the tick
            log.warning("heartbeat: most_important_facts failed: %s", exc)
            return None
        return rows

    def _mnemosyne_active(self) -> bool:
        """Heuristic: True when the active provider is the Mnemosyne adapter.

        The local provider stores a ``knowledge`` table but does not track
        ``last_accessed``; we only emit stale_fact findings when the real
        Mnemosyne backend is wired (it does track access times via the inner
        ``Mnemosyne`` instance)."""
        mem = getattr(self._hive, "memory", None)
        if mem is None:
            return False
        return getattr(mem, "name", "") == "mnemosyne"

    def _registered_patterns(self) -> set[tuple[str, ...]]:
        """Patterns already registered as learned skills (Pillar 3)."""
        store = getattr(self._hive, "learned_skills", None)
        if store is None or not hasattr(store, "list_by_status"):
            return set()
        try:
            rows = store.list_by_status(None) or []
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning("heartbeat: learned_skills.list_by_status failed: %s", exc)
            return set()
        out: set[tuple[str, ...]] = set()
        for tpl in rows:
            pat = tuple(getattr(tpl, "pattern", ()) or ())
            if pat:
                out.add(pat)
        return out

    def _scan_candidate_patterns(self) -> list[ProactiveFinding]:
        """Scan A: repeated tool sequences not yet learned."""
        findings: list[ProactiveFinding] = []
        try:
            from hive.tools import learned_skills as _ls
        except Exception as exc:  # noqa: BLE001 - missing optional dep
            log.warning("heartbeat: learned_skills module missing: %s", exc)
            return findings
        audit = getattr(self._hive, "audit_log", None)
        if audit is None or not hasattr(audit, "export"):
            return findings
        try:
            entries = audit.export(limit=200) or []
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning("heartbeat: audit_log.export failed: %s", exc)
            return findings
        try:
            patterns = _ls.detect_patterns(entries, min_repeats=2,
                                           min_seq_len=3, max_seq_len=5,
                                           limit=20) or []
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning("heartbeat: detect_patterns failed: %s", exc)
            return findings
        registered = self._registered_patterns()
        for pat, count in patterns:
            if pat in registered:
                continue
            findings.append(ProactiveFinding(
                type=TYPE_LEARNED_SKILL_CANDIDATE,
                data={"pattern": list(pat), "count": int(count)},
                priority=PRIORITY_MEDIUM,
            ))
        return findings

    def _scan_stale_facts(self, stale_days: int) -> list[ProactiveFinding]:
        """Scan B: facts in Mnemosyne that haven't been accessed for ``stale_days``."""
        if not self._mnemosyne_active():
            log.debug("heartbeat: proactive_scan stale_fact skipped (Mnemosyne unavailable)")
            return []
        facts = self._mnemosyne_facts()
        if facts is None:
            log.debug("heartbeat: proactive_scan stale_fact skipped (facts query returned None)")
            return []
        now_ts = time.time()
        cutoff = now_ts - stale_days * 86_400
        findings: list[ProactiveFinding] = []
        for f in facts:
            last = f.get("last_accessed")
            created = f.get("created_ts") or f.get("ts")
            if last is not None and last >= cutoff:
                continue
            # No access record: only stale when the row is older than the cutoff.
            if last is None and (created is None or created >= cutoff):
                continue
            anchor = last if last is not None else (created or now_ts)
            age_days = max(0, int((now_ts - anchor) // 86_400))
            findings.append(ProactiveFinding(
                type=TYPE_STALE_FACT,
                data={
                    "fact_id": str(f.get("id") or f.get("topic") or ""),
                    "topic": f.get("topic", ""),
                    "age_days": age_days,
                    "anchor": "last_accessed" if last is not None else "created",
                },
                priority=PRIORITY_LOW,
            ))
        return findings

    def _scan_stale_commitments(self, stale_days: int) -> list[ProactiveFinding]:
        """Scan C: active commitments whose next_due is in the past by >= stale_days."""
        book = getattr(self._hive, "commitments", None)
        if book is None or not hasattr(book, "upcoming"):
            return []
        findings: list[ProactiveFinding] = []
        now_ts = time.time()
        try:
            upcoming = book.upcoming(limit=50) or []
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning("heartbeat: commitments.upcoming failed: %s", exc)
            return findings
        for c in upcoming:
            try:
                # next_due_at may be None for inactive rows; skip those.
                due_ts = book.next_due_at(c.id)
            except Exception:  # noqa: BLE001 - defensive
                continue
            if due_ts is None:
                continue
            if due_ts > now_ts:
                continue
            days_overdue = (now_ts - due_ts) / 86_400
            if days_overdue < stale_days:
                continue
            findings.append(ProactiveFinding(
                type=TYPE_STALE_COMMITMENT,
                data={
                    "commitment_id": int(c.id),
                    "description": c.description,
                    "days_overdue": int(days_overdue),
                },
                priority=PRIORITY_HIGH,
            ))
        return findings

    def proactive_scan(self) -> list[ProactiveFinding]:
        """Run all proactive sub-scans; aggregate findings.

        Each sub-scan is wrapped in try/except so a single failure (e.g. Mnemosyne
        offline, audit_log missing) cannot abort the scan. Returns an empty list
        if no sub-scan produced anything.
        """
        cfg = self._hive.config
        stale_fact_days = int(getattr(cfg, "heartbeat_stale_fact_days", 30) or 30)
        stale_commit_days = int(
            getattr(cfg, "heartbeat_stale_commitment_days", 7) or 7)
        findings: list[ProactiveFinding] = []
        findings.extend(self._scan_candidate_patterns())
        findings.extend(self._scan_stale_facts(stale_fact_days))
        findings.extend(self._scan_stale_commitments(stale_commit_days))
        return findings

    def _emit_proactive_findings(self, findings: list[ProactiveFinding],
                                 now: float) -> int:
        """Push each finding onto the durable TaskBoard as a proactive_suggestion.

        Returns the number of enqueued tasks. The payload carries enough metadata
        for a downstream consumer to act on the suggestion (priority, type, data).
        """
        if not findings:
            return 0
        board = getattr(self._hive, "task_board", None)
        if board is None or not hasattr(board, "enqueue"):
            return 0
        # Supersede the previous scan's still-pending suggestions (nothing
        # consumes/completes these rows individually) so the board doesn't
        # accumulate one batch per proactive-scan interval forever.
        if hasattr(board, "bulk_cancel_pending"):
            board.bulk_cancel_pending(kind="proactive_suggestion")
        enq = 0
        for f in findings:
            payload = {
                "kind": "proactive_suggestion",
                "finding_type": f.type,
                "priority": f.priority,
                "data": f.data,
                "created_at": f.created_at.isoformat(),
                "emitted_at": now,
            }
            try:
                board.enqueue("proactive_suggestion", payload,
                              source="proactive_suggestion")
                enq += 1
            except Exception as exc:  # noqa: BLE001 - one bad enqueue must not abort
                log.warning("heartbeat: proactive enqueue failed: %s", exc)
        return enq

    async def _dispatch(self, tasks: list) -> int:
        board = self._hive.task_board

        async def run_one(record) -> bool:
            if not board.claim(record.id):
                return False  # already claimed by a concurrent drain
            payload = record.payload
            tool = payload.get("tool")
            if not tool:
                board.complete(record.id)  # nothing executable; consider it handled
                return False
            async with self._sem:
                try:
                    dispatch = await self._hive.tool_executor.execute(
                        tool, payload.get("args", {}), reason=payload.get("reason", ""))
                    # ToolExecutor reports expected failures as a structured
                    # dispatch rather than an exception. Never acknowledge a
                    # durable task until its tool actually ran.
                    if dispatch.status is DispatchStatus.PENDING:
                        approval_id = dispatch.approval_id
                        if not approval_id or not board.await_approval(record.id, approval_id):
                            board.fail_if_running(record.id, "approval dispatch could not be persisted")
                            log.warning("task %s could not await approval %s",
                                        record.id, approval_id)
                            return False
                        log.info("task %s is awaiting approval %s",
                                 record.id, approval_id)
                        return False
                    if dispatch.status is not DispatchStatus.OK:
                        detail = dispatch.error or f"tool dispatch {dispatch.status.value}"
                        board.fail(record.id, detail)
                        log.warning("task %s did not execute (%s): %s",
                                    record.id, dispatch.status.value, detail)
                        return False
                    board.complete(record.id)
                    return True
                except Exception as exc:  # noqa: BLE001 - one bad task must not abort the tick
                    board.fail(record.id, str(exc))
                    log.warning("task %s failed: %s", record.id, exc)
                    return False

        results = await asyncio.gather(*(run_one(t) for t in tasks))
        return sum(1 for ok in results if ok)

    async def _refresh_budget(self) -> None:
        cfg = self._hive.config
        await self._hive.budgeter.refresh(cfg.minimax_api_key, cfg.remains_url)

    async def _check_budget_alert(self) -> bool:
        """Run the spend-forecast Telegram alert (SPRINT_7 Batch F).

        Returns True when an alert was sent on this tick. The alert is only
        sent when the forecast status transitions into warn/critical/exceeded
        and the days_until_cap is at or below the configured threshold.
        """
        if self._budget_alert is None:
            from hive.autonomy.budget_alert import make_budget_alert
            self._budget_alert = make_budget_alert(self._hive)
        return await self._budget_alert.check()

    async def run(self, *, interval: float | None = None) -> None:
        if not self._hive.config.autonomy_enabled:
            log.warning("heartbeat loop not started: HIVE_AUTONOMY_ENABLED is false")
            return
        self._running = True
        period = interval if interval is not None else self._hive.config.heartbeat_sec
        # On startup, recover any tasks that were RUNNING when the process was killed.
        recovered = self._hive.task_board.requeue_running()
        if recovered:
            log.info("heartbeat: recovered %d RUNNING task(s) left from prior run", recovered)
        # Same idea for self-mod: reclaim any worktree/branch orphaned by a crash
        # mid-propose() (Batch I — P0 autonomy durable recovery).
        try:
            swept = await self._hive.self_modifier.sweep_orphaned_worktrees()
            if swept.get("removed"):
                log.info("heartbeat: swept %d orphaned self-mod worktree(s) from prior run",
                         len(swept["removed"]))
        except Exception as exc:  # noqa: BLE001 - startup sweep must not block the loop
            log.warning("heartbeat: orphaned worktree sweep failed: %s", exc)
        log.info("heartbeat loop started (interval=%ss)", period)
        while self._running:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must survive a bad tick
                log.error("heartbeat tick error: %s", exc, exc_info=True)
            await asyncio.sleep(period)

    def stop(self) -> None:
        self._running = False
