"""
runtime.py — HiveOS, the assembled runtime (composition root).

HiveOS = "hive operating system": the system that hosts the agent **Hive**. One
dataclass holds every wired subsystem; `HiveOS.build()` constructs them from the
typed config and injects the dependencies — clean DI without a container (pattern
from OpenJarvis system/{core,builder}.py, SYNTHESIS Part B; named to our identity
model — Hive is the agent, HiveOS is the system).

Placement note (deliberate refinement of the plan's `core/system.py` path): the
composition root imports every layer (llm/tools/memory/context/agents), so it CANNOT
live in `core` without breaking the core-is-a-leaf invariant the architecture test
enforces and that SYNTHESIS's own DAG mandates. It lives at the top level (peer of
the layers it wires); the DAG already treats this as what gateway/autonomy/surfaces
depend on.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hive.agents.board import BoardStore
from hive.agents.loop_guard import LoopGuard
from hive.agents.orchestrator import ConversationOrchestrator
from hive.agents.planner import Planner
from hive.autonomy.commitments import CommitmentBook
from hive.autonomy.cron import CronScheduler
from hive.autonomy.tasks import TaskBoard
from hive.context.session_store import SessionStore
from hive.core import credentials
from hive.core.budgeter import Budgeter
from hive.core.config import HiveConfig, set_config
from hive.core.events import EventBus, EventType
from hive.core.learning import (
    Evaluator as LearningEvaluator,
)
from hive.core.learning import (
    Evolver as LearningEvolver,
)
from hive.core.learning import (
    LearningLoop,
    LoopConfig,
)
from hive.core.learning import (
    Tracer as LearningTracer,
)
from hive.core.sandbox import make_sandbox_runner
from hive.core.self_mod import SelfModifier, github_pr_opener
from hive.core.spec_search import Edit, EditOutcome, SelfImprovement
from hive.core.types import Message, Role
from hive.llm.adapters import make_adapter
from hive.llm.credential_pool import CredentialPool
from hive.llm.host_bridge import HostLLMBridge
from hive.llm.model_catalog import ModelCatalog
from hive.llm.router import ModelRouter, TaskKind
from hive.memory.curator import Curator
from hive.memory.entity_resolver import EntityResolver
from hive.memory.keeper import MemoryKeeper
from hive.memory.local import LocalMemoryProvider
from hive.memory.mnemosyne_provider import build_mnemosyne_provider
from hive.memory.provider import MemoryProvider
from hive.memory.skill_usage import SkillUsageStore
from hive.memory.vault import ObsidianVault
from hive.observability.audit import AuditLog
from hive.observability.telemetry import Telemetry
from hive.observability.traces import TraceCollector
from hive.tools.base import BaseTool
from hive.tools.builtins import register_builtins
from hive.tools.executor import ToolExecutor
from hive.tools.learned_skills import STATUS_ARCHIVED as LS_STATUS_ARCHIVED
from hive.tools.learned_skills import STATUS_REGISTERED as LS_STATUS_REGISTERED
from hive.tools.learned_skills import LearnedSkillStore
from hive.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from hive.tools.mcp.server import MCPServer

log = logging.getLogger("hive.runtime")


def _load_entity_alias_map(spec: str) -> dict[str, str]:
    """Parse an inline JSON spec (or empty) into a dict for the entity resolver.

    The spec can be either:
      - an inline JSON object literal: ``'{"foo": "bar"}'``
      - a filesystem path that resolves to a JSON file
      - empty/None → returns {} so callers don't need to special-case it

    Bad inputs (malformed JSON, missing file) return {} and log a warning so a
    busted HIVE_ENTITY_RESOLUTION_ALIAS_MAP never breaks consolidation.
    """
    if not spec or not str(spec).strip():
        return {}
    raw = str(spec).strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            log.warning("entity alias map inline JSON invalid (%s) — ignoring", exc)
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    # Treat as path
    from pathlib import Path
    path = Path(raw)
    if not path.exists():
        log.warning("entity alias map path %s does not exist — ignoring", path)
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        log.warning("entity alias map file %s unreadable (%s) — ignoring", path, exc)
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


@dataclass(slots=True)
class HiveOS:
    config: HiveConfig
    events: EventBus
    router: ModelRouter
    tools: dict[str, BaseTool]
    tool_executor: ToolExecutor
    memory: MemoryProvider
    session_store: SessionStore
    keeper: MemoryKeeper
    planner: Planner
    orchestrator: ConversationOrchestrator
    budgeter: Budgeter
    telemetry: Telemetry
    traces: TraceCollector
    audit_log: AuditLog
    skill_usage: SkillUsageStore
    learned_skills: LearnedSkillStore
    curator: Curator
    self_modifier: SelfModifier
    learning_tracer: LearningTracer
    learning_evaluator: LearningEvaluator
    learning_evolver: LearningEvolver
    learning_loop: LearningLoop
    improver: SelfImprovement
    task_board: TaskBoard
    cron: CronScheduler
    commitments: CommitmentBook
    board: BoardStore
    agents_registry: dict  # name → AgentFactory; populated at build time
    edit_pending: dict    # approval_id → Edit; REVIEW-tier edits awaiting human approval
    host_llm: HostLLMBridge
    loop_guard: LoopGuard
    _gateway_lifecycle_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False,
    )
    _gateway_lifespans: int = field(default=0, init=False, repr=False)

    def acquire_gateway_lifespan(self) -> None:
        """Mark one FastAPI gateway app as using this shared runtime."""
        with self._gateway_lifecycle_lock:
            self._gateway_lifespans += 1

    def release_gateway_lifespan(self) -> bool:
        """Release one gateway app and return whether it owns final shutdown."""
        with self._gateway_lifecycle_lock:
            if self._gateway_lifespans <= 0:
                log.warning("gateway lifespan released without a matching acquire")
                return False
            self._gateway_lifespans -= 1
            return self._gateway_lifespans == 0

    async def ask(self, message: str, *, session_id: str = "default",
                  channel_hint: str = "") -> str:
        """End-to-end turn; returns the final assistant text."""
        result = await self.orchestrator.ask(message, session_id=session_id,
                                             channel_hint=channel_hint)
        return result.content

    async def consolidate(self, session_id: str = "default", *,
                          use_entity_resolution: bool | None = None) -> int:
        """Run sleep-time consolidation. SPRINT_7 Batch D defaults to ON.

        ``use_entity_resolution`` overrides default behaviour: True forces it,
        False disables it. When None (default) the flag tracks the config —
        ON when ``config.entity_resolution_enabled`` is True.
        """
        if use_entity_resolution is None:
            use_entity_resolution = bool(self.config.entity_resolution_enabled)
        return await self.keeper.consolidate(
            session_id, use_entity_resolution=use_entity_resolution,
        )

    async def title_session(self, session_id: str = "default") -> str | None:
        """Generate + store a short title from the session's first message (B3).
        Out-of-band (not in the hot turn path); idempotent; best-effort."""
        existing = self.session_store.get_title(session_id)
        if existing:
            return existing
        msgs = self.session_store.messages(session_id, limit=1)
        if not msgs:
            return None
        from hive.context.title import generate_title

        async def _summarize(m: list[Message], system: str) -> str:
            r = await self.router.complete(m, kind=TaskKind.AUX, system=system,
                                           thinking=False, max_tokens=64)
            return r.text

        title = await generate_title(msgs[0].content, _summarize)
        self.session_store.set_title(session_id, title)
        return title

    async def ask_stream(self, message: str, *, session_id: str = "default",
                         channel_hint: str = ""):
        """Stream a conversational reply token-by-token (SSE surface, M4 #sf-1).

        Direct model stream (SOUL + memory recall as context) — NOT the agentic tool
        loop, which stays on ask(). Persists the completed turn to the session store +
        memory after the stream finishes."""
        from hive.context.prompt_builder import build_messages, system_prompt

        mem_block = self.memory.system_prompt_block() if self.memory else ""
        recall = self.memory.prefetch(message, session_id=session_id) if self.memory else ""
        history = self.session_store.messages(session_id, limit=40) if self.session_store else []
        messages = build_messages(history, message, recall_block=recall)
        chunks: list[str] = []
        async for delta in self.router.stream(messages,
                                              system=system_prompt(mem_block,
                                                                   channel_hint=channel_hint)):
            chunks.append(delta)
            yield delta
        final = "".join(chunks)
        # Persist the turn (best-effort; never break a delivered stream).
        try:
            from hive.core.types import Role
            self.session_store.append(session_id, Role.USER, message)
            self.session_store.append(session_id, Role.ASSISTANT, final)
            self.memory.sync_turn(message, final, session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("ask_stream persist failed: %s", exc)

    async def stream_ask_iterations(
        self, message: str, *, session_id: str = "default", channel_hint: str = "",
    ):
        """Stream a tool-calling turn iteration-by-iteration (SPRINT_6 P-C).

        Yields orchestrator events (model_decision, tool_call_start,
        tool_call_end, loop_guard, final, max_turns, error) as they happen.
        Persistence happens inside the orchestrator, so callers don't need to
        do anything after draining the stream.
        """
        async for ev in self.orchestrator.stream_ask(
            message, session_id=session_id, channel_hint=channel_hint,
        ):
            yield ev

    def health(self) -> dict:
        """Return a full system health snapshot: task queue depth, budget usage,
        memory counts, telemetry, and registered tool count. Synchronous and safe
        to call at any time without touching the LLM."""
        from hive.core.approval import gate as _gate
        budget_snap = self.budgeter.snapshot()
        telemetry_snap = self.telemetry.snapshot()
        task_stats = self.task_board.statistics()
        mem_counts: dict = {}
        try:
            if hasattr(self.memory, "count"):
                mem_counts = self.memory.count()
        except Exception:  # noqa: BLE001
            pass
        try:
            pending_approvals = len(_gate.pending())
        except Exception:  # noqa: BLE001
            pending_approvals = -1
        return {
            "status": "ok",
            "tools": len(self.tools),
            "budget": budget_snap,
            "telemetry": telemetry_snap,
            "tasks": task_stats,
            "memory": mem_counts,
            "pending_approvals": pending_approvals,
            "pending_review_edits": self.improver.pending_count(),
            "cron_jobs": len(self.cron.jobs()),
            "active_commitments": self.commitments.count(active_only=True),
            "self_mod_proposals": len(self.self_modifier.history(limit=1000)),
        }

    def system_status(self) -> dict:
        """Broader system status view: router config, budget forecast, memory counts,
        task queue, and self-improvement state. Safe to call without a live model."""
        router_status: dict = {}
        try:
            if hasattr(self.router, "status"):
                router_status = self.router.status()
        except Exception:  # noqa: BLE001
            pass
        mem_counts: dict = {}
        try:
            if hasattr(self.memory, "count"):
                mem_counts = self.memory.count()
        except Exception:  # noqa: BLE001
            pass
        return {
            "router": router_status,
            "budget": self.budgeter.forecast(),
            "memory": mem_counts,
            "tasks": self.task_board.statistics(),
            "tools": self.tool_executor.stats(),
            "pending_approvals": self.improver.pending_count(),
            "self_mod_history_count": len(self.self_modifier.history(limit=1000)),
            "active_commitments": self.commitments.count(active_only=True),
            "cron_jobs": len(self.cron.jobs()),
        }

    def curate(self) -> dict:
        """Run the skill-lifecycle Curator (deterministic, safe). No-op until
        agent-created skills exist; built-in tools are exempt (registered as bundled)."""
        return self.curator.run()

    async def curate_umbrellas(self) -> dict:
        """Run the LLM umbrella-building step of the Curator (fail-open)."""
        try:
            return await self.curator.consolidate_umbrellas()
        except Exception as exc:  # noqa: BLE001
            log.warning("curate_umbrellas failed: %s", exc)
            return {"skipped": True, "reason": str(exc)}

    async def discover(self, need: str) -> dict:
        """Discovery-first (HARD SOUL rule): search official sources for an existing
        solution before building; cached via memory when supported (A1)."""
        from hive.tools import discovery
        mem = self.memory if (hasattr(self.memory, "recall")
                              and hasattr(self.memory, "learn")) else None
        return await discovery.discover(need, memory=mem,
                                        github_token=self.config.github_token)

    async def load_mcp_servers(self) -> int:
        """Connect configured MCP servers and register their tools into the live
        registry. A spec is either a stdio command line (HIVE_MCP_SERVERS) or an
        http(s):// URL (SSE, A6); MNEMOSYNE_MCP_URL is loaded as one such SSE server.
        Best-effort, per-server isolated (A2). Returns the number of tools loaded."""
        import shlex

        from hive.tools.mcp.client import MCPClient

        specs = list(self.config.mcp_servers)
        if self.config.mnemosyne_mcp_url:
            specs.append(self.config.mnemosyne_mcp_url)   # A6: remote Mnemosyne over MCP

        loaded = 0
        for spec in specs:
            if spec.startswith(("http://", "https://")):   # SSE transport
                client = MCPClient(url=spec)
                prefix = spec.rstrip("/").rsplit("/", 1)[-1] or "mcp"
            else:                                          # stdio transport
                parts = shlex.split(spec)
                if not parts:
                    continue
                client, prefix = MCPClient(parts[0], parts[1:]), parts[0]
            try:
                await client.connect()
                descriptors = await client.list_tools()
                for tool in client.as_tools(descriptors, prefix=f"{prefix}."):
                    self.tools[tool.spec.name] = tool
                    self.tool_executor.add_tool(tool)
                    loaded += 1
            except Exception as exc:  # noqa: BLE001 - one bad server must not block startup
                log.warning("MCP server %r failed to load: %s", spec, exc)
        if loaded:
            log.info("loaded %d MCP tool(s) from %d server(s)", loaded, len(specs))
        return loaded

    async def run_tests(self, *, test_cmd: str = "python -m pytest -q --tb=short",
                        timeout: float = 300.0) -> dict:
        """Run the project test suite and return structured results.

        Returns: {all_passed, passed, failed, errors, output, returncode, timed_out}
        Safe to call at any time; never triggers self-modification."""
        import asyncio as _asyncio
        import re as _re
        proc = await _asyncio.create_subprocess_shell(
            test_cmd, cwd=str(self.config.root),
            stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.STDOUT,
        )
        try:
            out_bytes, _ = await _asyncio.wait_for(proc.communicate(), timeout=timeout)
        except _asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return {"all_passed": False, "timed_out": True,
                    "output": f"Test suite timed out after {timeout:.0f}s",
                    "returncode": -1, "passed": 0, "failed": 0, "errors": 0}
        output = out_bytes.decode(errors="replace")
        rc = proc.returncode
        summary = _re.search(
            r"(\d+) passed(?:.*?(\d+) skipped)?(?:.*?(\d+) failed)?(?:.*?(\d+) error)?",
            output)
        passed = int(summary.group(1)) if summary else 0
        skipped_n = int(summary.group(2)) if (summary and summary.group(2)) else 0
        failed_n = int(summary.group(3)) if (summary and summary.group(3)) else 0
        errors_n = int(summary.group(4)) if (summary and summary.group(4)) else 0
        # Parse individual FAILED lines for structured failure context.
        failures = [
            {"test": m.group(1), "reason": m.group(2).strip() if m.group(2) else ""}
            for m in _re.finditer(r"FAILED\s+([\w/.::\[\]-]+)\s*(?:-\s*(.+))?", output)
        ]
        return {
            "all_passed": rc == 0,
            "returncode": rc,
            "passed": passed,
            "skipped": skipped_n,
            "failed": failed_n,
            "errors": errors_n,
            "failures": failures,
            "output": output[-3000:],
            "timed_out": False,
        }

    @staticmethod
    def _parse_test_output(raw: str) -> str:
        """Extract structured failure info from pytest output instead of blind tail-truncation."""
        lines = raw.splitlines()
        failed = [ln for ln in lines if ln.startswith("FAILED ")]
        summary_idx = next(
            (i for i, ln in enumerate(lines) if "short test summary" in ln.lower()), None
        )
        parts: list[str] = []
        if failed:
            parts.append("Failed tests:\n" + "\n".join(failed[:10]))
        if summary_idx is not None:
            parts.append("\n".join(lines[summary_idx: summary_idx + 20]))
        return ("\n\n".join(parts) or raw[-1000:])[:2000]

    async def _build_symptom_context(self, base: str = "") -> str:
        """Aggregate symptom data from audit, task failures, and prior failed proposals."""
        parts: list[str] = [base] if base else []
        try:
            rate = self.audit_log.error_rate(24.0)
            if rate > 0.05:
                stats = self.audit_log.stats()
                bad = [
                    f"  {t}: {info['by_status'].get('error', 0)}/{info['total']} errors"
                    for t, info in stats.get("by_tool", {}).items()
                    if info.get("by_status", {}).get("error", 0) > 0
                ]
                if bad:
                    parts.append("Tool errors (24h):\n" + "\n".join(bad[:5]))
        except Exception:  # noqa: BLE001
            pass
        try:
            fails = self.task_board.recent_failures(limit=5)
            if fails:
                parts.append("Recent task failures:\n" + "\n".join(
                    f"  [{f.kind}] {f.last_error or 'unknown'}" for f in fails
                ))
        except Exception:  # noqa: BLE001
            pass
        try:
            prior = self.self_modifier.failed_proposals(limit=3)
            if prior:
                parts.append("Prior failed proposals (do NOT repeat):\n" + "\n".join(
                    f"  {p.get('title', '')[:80]} — stage={p.get('stage', '?')}"
                    for p in prior
                ))
        except Exception:  # noqa: BLE001
            pass
        return "\n\n".join(parts) or "No specific symptoms detected."

    async def self_diagnose(self, *, dry_run: bool = False,
                            test_cmd: str = "python -m pytest -q --tb=short") -> dict:
        """Run tests → parse failures → trigger self-improvement cycle (SOUL.md safe).

        AUTO edits open draft PRs; REVIEW edits go to /approvals; Hive never merges.
        Returns a dict with test results + improvement outcomes."""
        test_result = await self.run_tests(test_cmd=test_cmd)
        if test_result.get("timed_out"):
            return {**test_result, "improvement_outcomes": [], "skipped_reason": None}
        if test_result["all_passed"]:
            log.info("self_diagnose: all tests pass — no self-improvement triggered")
            return {**test_result, "improvement_outcomes": [], "skipped_reason": None}
        # Budget guard: skip the LLM diagnoser call when we're near the daily cap.
        if self.budgeter.is_near_cap():
            log.warning("self_diagnose: near daily call cap — skipping LLM diagnoser")
            return {**test_result, "improvement_outcomes": [],
                    "skipped_reason": "near_daily_cap"}
        parsed_output = self._parse_test_output(test_result.get("output", ""))
        failures = test_result.get("failures", [])
        failure_summary = (
            ("\nFailed tests:\n" + "\n".join(f"  - {f['test']}" for f in failures[:20]))
            if failures else ""
        )
        base_symptom = (
            f"Test suite failure: {test_result['failed']} failed, "
            f"{test_result['errors']} errors.{failure_summary}\n"
            f"Test output:\n{parsed_output}"
        )
        symptom = await self._build_symptom_context(base_symptom)
        log.info("self_diagnose: triggering self-improvement (failed=%d)", test_result["failed"])
        outcomes = await self.self_improve_from_symptom(symptom, _already_enriched=True)
        return {
            **test_result,
            "improvement_outcomes": [
                {"status": o.status, "op": o.op.value, "tier": o.tier.value,
                 "detail": o.detail, "branch": o.branch}
                for o in outcomes
            ],
        }

    async def self_improve_from_symptom(self, symptom: str,
                                         *, _already_enriched: bool = False,
                                         use_learning_loop: bool = False) -> list:
        """Run a diagnosis-and-edit cycle for a detected symptom.

        Builds a minimal LLM-backed diagnoser from the current router, then runs
        the full spec_search loop. REVIEW/MANUAL tier edits are also enqueued as
        self_improve tasks so they appear in /tasks and /approvals.

        When ``use_learning_loop=True`` AND ``config.learning_loop_enabled``
        is true, the symptom is first routed through ``self.learning_loop``
        which gates the eventual edit with a pytest + golden_qa eval
        comparator. Returns an empty list in that case (the loop's
        LoopOutcome is persisted separately; callers should query the
        gateway ``/learning/history`` endpoint for the result)."""
        # --- Learning-loop early routing (SPRINT_6 P-F) -------------------
        # The caller explicitly opts in via ``use_learning_loop=True``. The
        # loop itself is gated by ``config.learning_loop_enabled`` (off by
        # default) so callers that don't know about the loop are unaffected.
        if use_learning_loop and self.config.learning_loop_enabled:
            from hive.core.types import LoopOutcome
            outcome: LoopOutcome = await self.learning_loop.run(symptom)
            log.info(
                "self_improve_from_symptom: routed via learning_loop "
                "(verdict=%s, branch=%s)",
                outcome.verdict, outcome.worktree_branch,
            )
            return []   # loop persisted its own LoopOutcome

        from hive.core.spec_search import Edit, EditOp, diagnose_and_run
        if not _already_enriched:
            symptom = await self._build_symptom_context(symptom)

        _OP_VALUES = {e.value for e in EditOp}
        _SCHEMA = (
            "Each edit MUST be a JSON object with:\n"
            '  "op": one of: ' + ", ".join(sorted(_OP_VALUES)) + "\n"
            '  "summary": short description (str)\n'
            '  "rationale": why this fixes the symptom (str)\n'
            "For edits to existing files also include:\n"
            '  "path": repo-relative file path\n'
            '  "old_text": exact text to replace (must match file content exactly)\n'
            '  "new_text": replacement text\n'
            "For CREATE_FILE ops:\n"
            '  "path": new file path\n'
            '  "new_text": complete file content\n'
            '  (old_text must be empty — CREATE_FILE never overwrites existing files)'
        )

        async def _diagnoser(context: str) -> list[Edit]:
            try:
                import json as _json
                # Rank source files by keyword relevance to the symptom.
                try:
                    ctx_lower = context.lower()
                    keywords = [w for w in ctx_lower.split() if len(w) > 4]

                    def _relevance(path_str: str) -> int:
                        pl = path_str.lower()
                        return sum(1 for kw in keywords if kw in pl)

                    all_py = [
                        str(p.relative_to(self.config.root))
                        for p in self.config.root.rglob("src/**/*.py")
                        if ".worktree" not in str(p)
                    ]
                    src_files = sorted(all_py, key=_relevance, reverse=True)[:30]
                    file_listing = "Source files (ranked by relevance):\n" + "\n".join(
                        f"  {f}" for f in src_files
                    )
                except Exception:  # noqa: BLE001
                    file_listing = ""
                # Anti-repetition: tell the LLM what NOT to try.
                avoid_hint = ""
                try:
                    prior = self.self_modifier.failed_proposals(limit=3)
                    if prior:
                        avoid_hint = "\nAVOID these previously tried approaches:\n" + "\n".join(
                            f"  - {p.get('title', '')[:80]} (failed at: {p.get('stage', '?')})"
                            for p in prior
                        )
                except Exception:  # noqa: BLE001
                    pass
                safe_ctx = context[:3000]
                prompt = (
                    "You are Hive's self-improvement diagnoser.\n"
                    "Analyse the symptom and propose zero or more typed edits as a JSON array.\n"
                    "Prefer ADD_TEST / CREATE_FILE (AUTO tier) for test gaps.\n"
                    "Use PATCH_CODE (REVIEW tier, needs human approval) for logic fixes.\n"
                    f"{avoid_hint}\n\n"
                    f"{file_listing}\n\nSymptom:\n{safe_ctx}"
                )
                res = await self.router.complete(
                    [Message(Role.USER, prompt)],
                    system=f"Return ONLY a valid JSON array of edit objects, or []. {_SCHEMA}",
                )
                raw_text = (res.text or "[]").strip()
                # Strip markdown code fences if present.
                if raw_text.startswith("```"):
                    raw_text = raw_text.split("```", 2)[1]
                    if raw_text.startswith(("json\n", "json ")):
                        raw_text = raw_text[4:]
                    raw_text = raw_text.rsplit("```", 1)[0]
                raw = _json.loads(raw_text.strip() or "[]")
                if not isinstance(raw, list):
                    return []
                edits: list[Edit] = []
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    try:
                        op = EditOp(item["op"])
                    except (KeyError, ValueError):
                        log.warning("diagnoser: unknown op %r — skipping", item.get("op"))
                        continue
                    path = item.get("path", "")
                    old_text = item.get("old_text", "")
                    new_text = item.get("new_text", "")

                    async def _apply(wt: str, _p: str = path,
                                     _old: str = old_text, _new: str = new_text,
                                     _op: EditOp = op) -> list[str]:
                        if not _p:
                            return []
                        from pathlib import Path as _Path
                        wt_root = _Path(wt).resolve()
                        target = (wt_root / _p).resolve()
                        # Strict containment: path must not escape the worktree.
                        try:
                            target.relative_to(wt_root)
                        except ValueError:
                            log.warning("diagnoser: path %r escapes worktree — skipping", _p)
                            return []
                        if _op is EditOp.CREATE_FILE:
                            # Safe: creates new files only, never overwrites.
                            if target.exists():
                                log.warning("diagnoser: CREATE_FILE target %r exists — skipping", _p)
                                return []
                            # Validate Python syntax before writing.
                            if _p.endswith(".py") and _new:
                                import ast as _ast
                                try:
                                    _ast.parse(_new)
                                except SyntaxError as se:
                                    log.warning("diagnoser: CREATE_FILE %r has syntax error — skipping: %s", _p, se)
                                    return []
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_text(_new, encoding="utf-8")
                            return [_p]
                        # All other ops: require old_text present in the target.
                        if not _old or not target.exists():
                            return []
                        content = target.read_text(encoding="utf-8")
                        if _old not in content:
                            log.debug("diagnoser: old_text not found in %r — skipping", _p)
                            return []
                        new_content = content.replace(_old, _new, 1)
                        # Validate Python syntax after patching .py files.
                        if _p.endswith(".py") and _new:
                            import ast as _ast
                            try:
                                _ast.parse(new_content)
                            except SyntaxError as se:
                                log.warning("diagnoser: patch creates syntax error in %r — skipping: %s", _p, se)
                                return []
                        target.write_text(new_content, encoding="utf-8")
                        return [_p]

                    # Pillar 4 safety checks (self_mod_safety.run_all_checks) only
                    # fire when target_files/code are populated — feed them here so
                    # they actually run against real proposals, not just the 50
                    # unit tests that construct Edit() manually. `code` is scoped to
                    # CREATE_FILE .py edits only: for PATCH_CODE etc., new_text is a
                    # replacement FRAGMENT (see _apply above), and ast.parse-ing a
                    # bare fragment routinely raises spurious SyntaxErrors even for
                    # a valid patch — which would false-positive-block legitimate
                    # patches at the critical/MANUAL tier before a human sees them.
                    edits.append(Edit(
                        op=op,
                        summary=item.get("summary", f"auto-edit: {op.value}"),
                        rationale=item.get("rationale", ""),
                        apply=_apply,
                        target_files=[path] if path else [],
                        code=(new_text if (op is EditOp.CREATE_FILE
                                           and path.lower().endswith(".py") and new_text)
                             else None),
                    ))
                return edits
            except Exception as exc:  # noqa: BLE001
                log.error("_diagnoser failed (symptom=%r): %s",
                          symptom[:100] if symptom else "", exc, exc_info=True)
                return []

        try:
            outcomes = await diagnose_and_run(_diagnoser, symptom, self.improver)
        except Exception as exc:  # noqa: BLE001 - self-improve must never crash callers
            log.error("self_improve_from_symptom: diagnose_and_run raised: %s", exc,
                      exc_info=True)
            return []
        from hive.core.spec_search import RiskTier
        for outcome in outcomes:
            if outcome.tier in (RiskTier.REVIEW, RiskTier.MANUAL):
                self.task_board.enqueue(
                    "self_improve",
                    {"symptom": symptom[:200], "tier": outcome.tier.value,
                     "op": outcome.op.value, "edit_id": outcome.edit_id,
                     "detail": outcome.detail[:300],
                     "approval_id": outcome.approval_id,
                     "status": outcome.status},
                    source="heartbeat",
                )
            # NOTE: outcome memory recording (success:<op> / failure:<stage> /
            # failure:protected) now lives inside SelfImprovement itself
            # (self.improver is built with memory_provider=self.memory), so
            # both this AUTO path and the REVIEW-approved path record
            # identically — see SelfImprovement._record_outcome.
        return outcomes

    def mcp_server(self, *, name: str = "hive") -> "MCPServer":
        """Return an MCPServer that exposes the live tool registry over MCP stdio.
        Lazy import keeps the mcp SDK optional at runtime."""
        from hive.tools.mcp.server import MCPServer
        return MCPServer(self.tools, name=name)

    async def serve_mcp(self) -> None:  # pragma: no cover - needs the mcp SDK
        """Expose Hive's tools to other agents over MCP stdio (`hive mcp-serve`)."""
        await self.mcp_server().serve_stdio()

    async def self_improve(self, edits: list[Edit], *, dry_run: bool = False,
                           ) -> list[EditOutcome]:
        """Drive proposed edits through the risk gate (AUTO->PR / REVIEW->approval /
        MANUAL->recorded). Hive NEVER merges; AUTO edits open a draft PR for a human."""
        return await self.improver.run(edits, dry_run=dry_run)

    def abort_self_mod(self, approval_id: str) -> bool:
        """Cancel a single pending REVIEW-tier self-mod edit by approval_id.

        Removes it from both the improver's pending store and edit_pending.
        Returns False if the approval_id is not found."""
        removed = self.improver.cancel_review(approval_id)
        self.edit_pending.pop(approval_id, None)
        return removed

    def abort_all_self_mods(self) -> int:
        """Cancel all pending REVIEW-tier self-mod edits. Returns count cancelled."""
        count = self.improver.cancel_all_pending()
        self.edit_pending.clear()
        return count

    def last_self_mod_branch(self) -> str | None:
        """Return the branch name from the most recent successful self-mod proposal, or None."""
        result = self.self_modifier.last_result
        if result and result.get("ok") and result.get("branch"):
            return result["branch"]
        return None

    def pending_review_edits(self) -> list[dict]:
        """Return a list of pending REVIEW-tier edits awaiting human approval.

        Each dict has: approval_id, edit_id, op, summary, rationale."""
        pending = self.improver.get_all_pending()
        return [
            {"approval_id": aid, "edit_id": e.id,
             "op": e.op.value, "summary": e.summary, "rationale": e.rationale}
            for aid, e in pending.items()
        ]

    def self_mod_history(self, limit: int = 20) -> list[dict]:
        """Return the most recent self-mod proposal outcomes (newest first)."""
        return self.self_modifier.history(limit=limit)

    def recent_self_mod_branches(self, n: int = 5) -> list[str]:
        """Return up to n branch names from recent successful self-mod proposals."""
        return self.self_modifier.recent_branches(n=n)

    def resume_after_restart(self) -> dict:
        """Recover tasks left in RUNNING state after an unclean shutdown.

        Returns a dict with the count of tasks requeued back to pending."""
        requeued = self.task_board.requeue_running()
        return {"requeued": requeued}

    def event_history(self, n: int = 20) -> list[dict]:
        """Return the n most recent EventBus events (newest first)."""
        return self.events.recent_events(n=max(1, min(n, 500)))

    def loop_guard_stats(self) -> dict:
        """Return current LoopGuard statistics (call counts, tool usage)."""
        return self.loop_guard.stats()

    def reset_loop_guard(self) -> None:
        """Reset the LoopGuard state (call history and per-tool counts)."""
        self.loop_guard.reset()

    async def aclose(self) -> None:
        close_router = getattr(self.router, "aclose", None)
        if close_router is not None:
            await close_router()
        # Graceful memory shutdown (both provider types expose close/on_session_end).
        mem_close = getattr(self.memory, "close", None)
        if mem_close is not None:
            mem_close()
        self.session_store.close()
        self.skill_usage.close()
        self.task_board.close()
        self.cron.close()
        self.commitments.close()
        self.host_llm.close()      # stop the dedicated host-LLM loop (no-op if unused)
        self.audit_log.close()

    @classmethod
    def build(cls, config: HiveConfig | None = None, *,
              router: ModelRouter | None = None) -> "HiveOS":
        """Construct + wire every subsystem. Inject `router` to bypass the network in tests."""
        cfg = config or HiveConfig.from_env()
        if cfg.production_mode and (not cfg.secret.strip() or cfg.secret == "change_me"):
            raise RuntimeError(
                "HIVE_PRODUCTION=true requires a non-empty HIVE_SECRET different from 'change_me'"
            )
        if cfg.autonomy_enabled and not cfg.approver_key:
            raise RuntimeError(
                "HIVE_AUTONOMY_ENABLED=true requires HIVE_APPROVER_KEY to be configured"
            )
        if cfg.telegram_token and not cfg.telegram_webhook_secret:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN requires TELEGRAM_WEBHOOK_SECRET to be configured"
            )
        if cfg.telegram_token and not (
            cfg.telegram_allowed_user_ids or cfg.telegram_allowed_chat_ids
        ):
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN requires HIVE_TELEGRAM_ALLOWED_USER_IDS or "
                "HIVE_TELEGRAM_ALLOWED_CHAT_IDS to be configured"
            )
        if cfg.slack_signing_secret and not cfg.slack_allowed_user_ids:
            raise RuntimeError(
                "HIVE_SLACK_SIGNING_SECRET requires HIVE_SLACK_ALLOWED_USER_IDS to be configured"
            )
        if cfg.discord_public_key and not cfg.discord_allowed_user_ids:
            raise RuntimeError(
                "HIVE_DISCORD_PUBLIC_KEY requires HIVE_DISCORD_ALLOWED_USER_IDS to be configured"
            )
        if cfg.smtp_webhook_secret and not cfg.email_allowed_senders:
            raise RuntimeError(
                "HIVE_SMTP_WEBHOOK_SECRET requires HIVE_EMAIL_ALLOWED_SENDERS to be configured"
            )
        if cfg.autonomous_selfmod_enabled and not cfg.sandbox_image:
            raise RuntimeError(
                "HIVE_AUTONOMOUS_SELFMOD_ENABLED=true requires HIVE_SANDBOX_IMAGE to be configured"
            )
        cfg.ensure_dirs()
        set_config(cfg)                       # make get_config() return the built config (D1)
        credentials.inject()                   # populate env from the 0o600 vault (A4)
        # The global operational wrapper owns only state around the immutable gate.
        # Binding it here makes restart rehydration use the assembled runtime's DB.
        from hive.core.approval_enhancements import enhance
        enhance.configure_persistence(str(cfg.state_db))
        events = EventBus()                    # each assembled HiveOS owns its bus (no cross-talk)

        # Budget guard: sync gate for the router; record_call on every successful call.
        budgeter = Budgeter(daily_cap=cfg.daily_call_cap,
                           daily_spend_cap_usd=cfg.budget_daily_spend_cap_usd,
                           warn_pct=cfg.window_warn_pct,
                           history_path=str(cfg.data_dir / "budget_history.json"))
        events.subscribe(EventType.INFERENCE_END, budgeter.record_call)
        events.subscribe(EventType.INFERENCE_END, budgeter.record_usage)  # per-token cost
        telemetry = Telemetry().attach(events)
        traces = TraceCollector().attach(events)

        catalog = ModelCatalog()
        # M8: pick the executor provider (minimax|anthropic) from config; A4: pool keys
        # from the 0o600 vault merged with env, comma-split for multi-key failover.
        if cfg.exec_provider.lower() == "anthropic":
            exec_base, key_env, key_default = (cfg.anthropic_base, "ANTHROPIC_API_KEY",
                                               cfg.anthropic_api_key)
        else:
            exec_base, key_env, key_default = (cfg.minimax_anthropic_base, "MINIMAX_API_KEY",
                                               cfg.minimax_api_key)
        raw_key = credentials.get(key_env, key_default) or ""
        exec_keys = [k.strip() for k in raw_key.split(",") if k.strip()] or [key_default]
        router = router or ModelRouter(
            config=cfg,
            adapter=make_adapter(cfg.exec_provider, base_url=exec_base, catalog=catalog),
            credential_pool=CredentialPool(exec_keys),
            catalog=catalog,
            events=events,
            budget=budgeter.gate,
        )

        # Shared state DB holds memory + session tables (OpenClaw: one shared state DB).
        # Memory provider: real Mnemosyne when installed/configured, else local SQLite.
        # A3: give Mnemosyne a host-LLM backend so its consolidation reuses HiveOS's
        # provider (own isolated loop/client — never touches the main event loop).
        host_llm = HostLLMBridge(provider=cfg.exec_provider, base_url=exec_base,
                                 api_key=exec_keys[0] if exec_keys else "",
                                 model=cfg.aux_model, catalog=catalog)
        _mnem = build_mnemosyne_provider(home=cfg.mnemosyne_home)
        if _mnem is None:
            log.warning(
                "Mnemosyne not available — running with LocalMemoryProvider (degraded memory). "
                "Install mnemosyne-memory for full memory capabilities."
            )
            events.publish(EventType.MEMORY_STORE, {"status": "degraded", "provider": "local"})
        memory: MemoryProvider = (
            _mnem or LocalMemoryProvider(cfg.state_db, vault=ObsidianVault(cfg.obsidian_vault),
                                         bus=events)
        )
        # M9-b: wire host-LLM backend so Mnemosyne consolidation gets LLM backing.
        # A dedicated asyncio loop + daemon thread avoids cross-loop httpx reuse.
        from hive.memory.mnemosyne_provider import HiveMnemosyneProvider
        if isinstance(memory, HiveMnemosyneProvider):
            aux_adapter = make_adapter(cfg.exec_provider, base_url=exec_base, catalog=catalog)
            memory.set_host_llm_backend(
                aux_adapter, cfg.aux_model,
                api_key=exec_keys[0] if exec_keys else "",
            )
        session_store = SessionStore(cfg.state_db)

        # Fresh per-build tool registry so repeated build() calls don't collide.
        class _Registry(ToolRegistry):
            pass
        # N-2: shell provider selection (local by default; docker for container isolation).
        from hive.tools.shell_provider import DockerShellProvider, LocalShellProvider
        if cfg.shell_provider == "docker":
            _shell_provider = DockerShellProvider(image=cfg.shell_docker_image)
        else:
            _shell_provider = LocalShellProvider()
        # M3 task board created early so create_task tool can reference it at registration.
        task_board = TaskBoard(cfg.state_db)
        # A1: the discovery-first tool gets memory (for caching) + Hive's GitHub token.
        # query_memory + create_task get memory and task_board for mid-turn reactive access.
        tools = register_builtins(_Registry, memory=memory, task_board=task_board,
                                  events=events,
                                  github_token=cfg.github_token,
                                  github_owner=cfg.github_owner, github_repo=cfg.github_repo,
                                  telegram_token=cfg.telegram_token,
                                  smtp_host=cfg.smtp_host, smtp_port=cfg.smtp_port,
                                  smtp_user=cfg.smtp_user, smtp_pass=cfg.smtp_pass,
                                  smtp_to=cfg.smtp_to, slack_webhook=cfg.slack_webhook,
                                  discord_webhook=cfg.discord_webhook,
                                  vault_path=cfg.obsidian_vault,
                                  shell_provider=_shell_provider,
                                  deploy_ssh_host=cfg.deploy_ssh_host,
                                  deploy_ssh_key=cfg.deploy_ssh_key,
                                  stripe_secret_key=cfg.stripe_secret_key,
                                  stripe_customer_id=cfg.stripe_customer_id)
        audit_log = AuditLog(cfg.data_dir / "audit.sqlite")
        _tool_timeout = cfg.tool_timeout if cfg.tool_timeout > 0 else None
        tool_executor = ToolExecutor(tools, events=events, audit=audit_log.record,
                                     timeout=_tool_timeout)

        # Aux-model summarizer wired here so memory/context never import llm (strict DAG).
        async def summarize(messages: list[Message], system: str) -> str:
            result = await router.complete(messages, kind=TaskKind.AUX, system=system,
                                           thinking=False, max_tokens=2048)
            return result.text

        # SPRINT_7 Batch D — wire entity resolution into the keeper.
        # Resolver is built only when the operator has not explicitly disabled it.
        alias_map = _load_entity_alias_map(cfg.entity_resolution_alias_map) \
            if cfg.entity_resolution_enabled else {}
        entity_resolver = EntityResolver(alias_map=alias_map) \
            if cfg.entity_resolution_enabled else None
        keeper = MemoryKeeper(summarize, memory, resolver=entity_resolver)
        planner = Planner(router)
        orchestrator = ConversationOrchestrator(
            router, tools=tools, tool_executor=tool_executor,
            memory=memory, session_store=session_store, events=events,
            summarizer=summarize,
            max_iterations=cfg.max_iterations, max_per_tool=cfg.max_per_tool,
            planner=planner,
            goals=["Assist the user effectively", "Continuously improve HiveOS"],
        )

        # M2 self-improvement: skill lifecycle + risk-gated self-mod (all on the
        # existing safety spine — AUTO opens a draft PR, REVIEW hits the approval
        # gate, Hive never merges).
        skill_usage = SkillUsageStore(cfg.state_db)
        # Built-in tools are bundled, not agent-created — register them as such so the
        # Curator's lifecycle (stale->archived) can NEVER touch them.
        for name in tools:
            skill_usage.register(name, agent_created=False)

        def _record_skill_use(event: object) -> None:
            from collections.abc import Mapping

            raw = getattr(event, "data", event) or {}
            data = raw if isinstance(raw, Mapping) else {}
            if data.get("status") == "ok" and data.get("tool"):
                skill_usage.record_use(str(data["tool"]))
        events.subscribe(EventType.TOOL_CALL_END, _record_skill_use)

        # PILLAR 3 (sprint7): learned-skill store (proposed/approved/registered templates)
        learned_skills = LearnedSkillStore(cfg.state_db)

        # Curator age-out (SPRINT_7 risk: "never delete + auto-create = growth")
        # needs to actually remove an archived learned skill from the LIVE
        # registry, not just flag its skill_usage row — otherwise it stays
        # callable/prompt-visible forever. These closures are the only place
        # allowed to bridge hive.memory (Curator) to hive.tools (the concrete
        # registry/executor/LearnedSkill types), since hive.memory must not
        # import hive.tools (DAG, test_architecture.py).
        #
        # They also keep LearnedSkillStore's own `status` field honest: without
        # this, GET /skills/learned?status=registered would list an archived
        # (deregistered) skill as "registered" forever, since skill_usage.state
        # and learned_skills.status are two independent tracking tables that
        # would otherwise silently drift apart the moment the Curator ages
        # something out (Batch K — found as a non-blocking gap during Batch H's
        # own PR audit).
        def _deregister_skill(name: str) -> None:
            tools.pop(name, None)
            tool_executor.remove_tool(name)
            template = learned_skills.get(name)
            if template is not None and template.status == LS_STATUS_REGISTERED:
                learned_skills.update_status(name, LS_STATUS_ARCHIVED)

        def _reregister_skill(name: str) -> bool:
            template = learned_skills.get(name)
            if template is None or template.status not in (LS_STATUS_REGISTERED,
                                                            LS_STATUS_ARCHIVED):
                return False
            from hive.tools.learned_skills import LearnedSkill
            skill = LearnedSkill(template, registry=tools, executor=tool_executor)
            tools[name] = skill
            tool_executor.add_tool(skill)
            if template.status != LS_STATUS_REGISTERED:
                learned_skills.update_status(name, LS_STATUS_REGISTERED)
            return True

        curator = Curator(skill_usage, backup_dir=cfg.data_dir / "backups" / "skills",
                          summarize=summarize, deregister=_deregister_skill,
                          reregister=_reregister_skill)
        # Real PR opener only when Hive's GitHub identity is configured; else None
        # (SelfModifier still pushes the branch — a human opens the PR).
        opener = None
        if cfg.github_token and cfg.github_owner and cfg.github_repo:
            opener = github_pr_opener(cfg.github_token, cfg.github_owner, cfg.github_repo)
        # Optional sandbox: run candidate test suites in a container (HIVE_SANDBOX_IMAGE).
        # With no image this is the plain local runner.
        sandbox_run = make_sandbox_runner(cfg.sandbox_image or None, repo_root=str(cfg.root))
        self_modifier = SelfModifier(repo_root=str(cfg.root), open_pr=opener, run=sandbox_run,
                                     bus=events)
        edit_pending: dict = {}
        improver = SelfImprovement(
            self_modifier,
            pending_store=edit_pending,
            audit=audit_log.record,
            memory_provider=memory,
            safety_enabled=cfg.selfmod_enable_safety_checks,
            safety_max_files=cfg.selfmod_safety_max_files,
        )

        # M3 autonomy: cron + commitments (task_board already created above for builtins).
        cron = CronScheduler(cfg.state_db, task_board)
        commitments = CommitmentBook(cfg.state_db, task_board)
        # SPRINT_6 P-G Kanban (issue #75): subscribe BoardStore to A2A lifecycle events.
        board = BoardStore(events)

        # Named agent registry: allows delegate_named(task, "researcher") by name.
        from hive.agents.delegate import register_agent

        def _leaf_factory(agent_name: str):
            def factory() -> ConversationOrchestrator:  # type: ignore[name-defined]
                return ConversationOrchestrator(
                    router, tools=tools, tool_executor=tool_executor,
                    memory=memory, session_store=session_store, events=events,
                    max_iterations=cfg.max_iterations, max_per_tool=cfg.max_per_tool,
                )
            factory.__name__ = agent_name
            return factory

        _specialist_names = [
            "researcher", "coder", "reviewer", "memory-keeper", "security-reviewer",
        ]
        agents_registry: dict = {}
        for _name in _specialist_names:
            _factory = _leaf_factory(_name)
            register_agent(_name, _factory)
            agents_registry[_name] = _factory

        log.info("HiveOS built (tools=%d, exec_model=%s)", len(tools), cfg.exec_model)

        # --- Learning loop wiring (SPRINT_6 P-F) --------------------------
        # Always construct the modules — they're cheap and table-create
        # on first record(). Behaviour is gated by LoopConfig.enabled
        # (mirrors config.learning_loop_enabled), defaulting OFF so the
        # existing self-improve flow is unchanged.
        _learning_db_path = str(cfg.state_db)
        learning_tracer = LearningTracer(_learning_db_path)
        learning_evaluator = LearningEvaluator(
            repo_root=str(cfg.root),
            timeout_seconds=cfg.learning_eval_timeout,
        )
        learning_evolver = LearningEvolver(self_modifier, db_path=_learning_db_path)
        learning_loop = LearningLoop(
            tracer=learning_tracer,
            evolver=learning_evolver,
            evaluator=learning_evaluator,
            config=LoopConfig(
                enabled=cfg.learning_loop_enabled,
                eval_timeout=cfg.learning_eval_timeout,
                repo_root=str(cfg.root),
                db_path=_learning_db_path,
                autopromote=False,  # off by default; operator opt-in only
            ),
        )

        hive = cls(
            config=cfg, events=events, router=router, tools=tools,
            tool_executor=tool_executor, memory=memory, session_store=session_store,
            keeper=keeper, planner=planner, orchestrator=orchestrator,
            budgeter=budgeter, telemetry=telemetry, traces=traces, audit_log=audit_log,
            skill_usage=skill_usage, curator=curator, self_modifier=self_modifier,
            learned_skills=learned_skills,
            improver=improver, task_board=task_board, cron=cron, commitments=commitments,
            agents_registry=agents_registry, edit_pending=edit_pending,
            board=board,
            host_llm=host_llm,
            loop_guard=LoopGuard(max_per_tool=cfg.max_per_tool),
            learning_tracer=learning_tracer,
            learning_evaluator=learning_evaluator,
            learning_evolver=learning_evolver,
            learning_loop=learning_loop,
        )
        # Wire HiveStatus post-construction (needs the fully-built hive reference).
        status_tool = hive.tools.get("hive_status")
        if status_tool is not None:
            status_tool._hive = hive  # type: ignore[attr-defined]
        return hive
