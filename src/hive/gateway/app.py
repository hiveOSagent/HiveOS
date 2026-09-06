"""
app.py — HiveOS gateway (FastAPI, KEEP+IMPROVE from Gateway/app.py).

`create_app(hive)` builds the app around an assembled HiveOS, so the gateway holds
no globals and is trivially testable with Starlette's TestClient. Surfaces
(terminal/dashboard/voice/telegram) reach Hive through:
  GET  /health                 — liveness
  POST /chat                   — one turn (auth)
  POST /chat/stream            — SSE token stream (auth, M4 #sf-1)
  WS   /ws                     — streaming-ish chat loop (token handshake)
  GET  /budget                 — budgeter snapshot (auth)
  GET  /telemetry              — model/token/cost counters (auth, M10-a)
  GET  /traces/{session_id}    — per-session event trace (auth, M10-a)
  GET  /audit                  — recent tool-call audit entries (auth, M10-a)
  GET  /tasks                  — task board state (auth, M10-a)
  GET  /approvals              — pending danger-gated calls (auth)
  POST /approvals/decide       — approve/deny; approval runs the gated tool (auth)
  GET  /app/*                  — Mission Control dashboard SPA (if dashboard/dist built)
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import queue
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from hive.core.approval import gate
from hive.core.approval_enhancements import DecisionOutcome, enhance
from hive.gateway.auth import make_approver_dependency, make_auth_dependency, token_ok
from hive.gateway.channels.base import ChannelAdapter, MessageEvent, OutgoingMessage
from hive.gateway.channels.telegram import TelegramChannel
from hive.gateway.protocol import ApprovalDecision, ChatRequest, ChatResponse
from hive.runtime import HiveOS
from hive.tools.executor import DispatchStatus

# Cap on inbound webhook body (1 MiB) to bound parse + signature work per request.
MAX_WEBHOOK_BODY = 1_048_576

# Dashboard dist path: src/hive/gateway/ → repo root / dashboard/dist
_DASHBOARD_DIST = Path(__file__).parent.parent.parent.parent / "dashboard" / "dist"

log = logging.getLogger("hive.gateway")


def _sender_allowed(event: MessageEvent, *, allowed_users: frozenset[str],
                    allowed_chats: frozenset[str] = frozenset(),
                    casefold: bool = False) -> bool:
    """Return whether an inbound event belongs to an explicitly allowed owner."""
    normalize = str.casefold if casefold else str
    user_id = normalize(event.user_id.strip()) if event.user_id else ""
    chat_id = normalize(event.chat_id.strip()) if event.chat_id else ""
    return user_id in allowed_users or chat_id in allowed_chats


def _reject_unallowed_sender(
    event: MessageEvent, *, reason: str = "sender_not_allowed"
) -> dict[str, object]:
    """Acknowledge untrusted webhook input without placing it in a model turn."""
    event.trust = "untrusted"
    log.warning(
        "%s webhook: refused sender reason=%s trust=untrusted",
        event.platform,
        reason,
    )
    return {"ok": True, "handled": False, "reason": reason}


def create_app(hive: HiveOS, *, telegram: ChannelAdapter | None = None) -> FastAPI:
    cfg = hive.config
    secret = cfg.secret
    require_token = make_auth_dependency(secret)
    approver_key = cfg.approver_key
    if not approver_key and not cfg.autonomy_enabled:
        log.warning(
            "HIVE_APPROVER_KEY is not configured; supervised approvals temporarily "
            "fall back to HIVE_SECRET. Configure HIVE_APPROVER_KEY before enabling autonomy."
        )
        approver_key = secret
    require_approver = make_approver_dependency(approver_key)
    # Telegram surface (optional): use an injected channel, else build one from config.
    if telegram is None and hive.config.telegram_token:
        telegram = TelegramChannel(hive.config.telegram_token)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await hive.load_mcp_servers()   # connect configured MCP servers (best-effort, A2)
        log.info("HiveOS gateway online")
        yield
        await hive.aclose()
        log.info("HiveOS gateway offline")

    app = FastAPI(title="HiveOS Gateway", lifespan=lifespan)
    _cors_origins = [o.strip() for o in cfg.cors_origins.split(",") if o.strip()] if cfg.cors_origins != "*" else ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Hive-Token", "X-Session-Id", "x-hive-iterations"],
    )

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        return response

    @app.get("/health")
    async def health() -> dict:
        from hive.gateway.protocol import PROTOCOL_VERSION
        return {"status": "ok", "service": "hiveos-gateway",
                "protocol_version": PROTOCOL_VERSION}

    @app.get("/health/full", dependencies=[Depends(require_token)])
    async def health_full() -> dict:
        """Full system health snapshot including budget, tasks, memory, and telemetry."""
        return hive.health()

    @app.get("/health/summary", dependencies=[Depends(require_token)])
    async def health_summary() -> dict:
        """Concise health snapshot: budget warning, task queue state, cron, self-mod, error rate."""
        budget_warn = hive.budgeter.warning_status()
        return {
            "budget": {
                "warning": budget_warn,
                "calls_today": hive.budgeter.snapshot()["calls_today"],
                "remaining_calls": hive.budgeter.remaining_calls(),
                "calls_per_hour": hive.budgeter.calls_per_hour(),
            },
            "tasks": {
                "pending": hive.task_board.pending_count(),
                "running": hive.task_board.running_count(),
                "failed": hive.task_board.failed_count(),
                "pending_by_kind": hive.task_board.pending_by_kind(),
                "avg_age_pending_secs": hive.task_board.average_age_pending(),
            },
            "cron": hive.cron.job_health(),
            "self_mod": {
                "proposals": hive.self_modifier.proposal_count(),
                "success_rate": hive.self_modifier.success_rate(),
                "recent_branches": hive.self_modifier.recent_branches(n=3),
            },
            "audit": {
                "error_rate_24h": hive.audit_log.error_rate(window_hours=24.0),
            },
            "channels": {
                "telegram": bool(getattr(hive.config, "telegram_token", None)),
                "slack": bool(getattr(hive.config, "slack_signing_secret", None)
                              and getattr(hive.config, "slack_bot_token", None)),
                "discord": bool(getattr(hive.config, "discord_bot_token", None)),
                "email": bool(getattr(hive.config, "smtp_host", None)
                              and getattr(hive.config, "smtp_user", None)),
            },
        }

    @app.post("/chat", response_model=ChatResponse, dependencies=[Depends(require_token)])
    async def chat(body: ChatRequest) -> ChatResponse:
        try:
            reply = await hive.ask(body.message, session_id=body.session_id,
                                   channel_hint="web")
        except Exception as exc:  # noqa: BLE001
            log.error("chat turn failed (session=%s): %s", body.session_id, exc, exc_info=True)
            raise HTTPException(status_code=503, detail="internal error") from exc
        return ChatResponse(reply=reply, session_id=body.session_id)

    @app.post("/chat/stream", dependencies=[Depends(require_token)])
    async def chat_stream(body: ChatRequest) -> StreamingResponse:
        """SSE token stream of a conversational reply (M4 #sf-1). Each token is one
        `data:` event; the stream ends with `data: [DONE]`."""
        async def events():
            try:
                async for delta in hive.ask_stream(body.message, session_id=body.session_id,
                                                  channel_hint="web"):
                    yield f"data: {delta}\n\n"
            except Exception as exc:  # noqa: BLE001 - surface as a terminal SSE error
                log.error("stream error (session=%s): %s", body.session_id, exc, exc_info=True)
                safe_name = type(exc).__name__.replace("\n", " ").replace("\r", " ")
                yield f"event: error\ndata: {safe_name}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/chat/stream/iterations", dependencies=[Depends(require_token)])
    async def chat_stream_iterations(body: ChatRequest) -> StreamingResponse:
        """SSE stream of orchestrator iteration events (SPRINT_6 P-C).

        Unlike `/chat/stream` (which emits raw tokens from a direct model call),
        this runs the full agentic tool loop and yields per-iteration events:
        `model_decision`, `tool_call_start`, `tool_call_end`, `loop_guard`,
        `final` / `max_turns` / `error`. Each event is `event: <type>\\ndata: <json>\\n\\n`;
        the stream ends with `data: [DONE]`.
        """
        import json as _json

        async def events():
            try:
                async for ev in hive.stream_ask_iterations(
                    body.message, session_id=body.session_id, channel_hint="web",
                ):
                    ev_type = ev.get("type", "event")
                    payload = _json.dumps(ev, default=str)
                    yield f"event: {ev_type}\ndata: {payload}\n\n"
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "iterations stream error (session=%s): %s",
                    body.session_id, exc, exc_info=True,
                )
                safe_name = type(exc).__name__.replace("\n", " ").replace("\r", " ")
                yield f"event: error\ndata: {safe_name}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.get("/budget", dependencies=[Depends(require_token)])
    async def budget() -> dict:
        return hive.budgeter.snapshot()

    @app.get("/budget/detail", dependencies=[Depends(require_token)])
    async def budget_detail() -> dict:
        snap = hive.budgeter.snapshot()
        return {**snap,
                "remaining_calls": hive.budgeter.remaining_calls(),
                "is_near_cap": hive.budgeter.is_near_cap()}

    @app.get("/budget/forecast", dependencies=[Depends(require_token)])
    async def budget_forecast(days: int = 7) -> dict:
        """Linear projection of budget spend over the next `days` (SPRINT_7 Batch F).

        Returns: projected_total, daily_avg, max_daily, days_until_cap, status,
        confidence. Status is "ok" / "warn" / "critical" / "exceeded". Empty
        history returns safe defaults (status="ok", days_until_cap=None).
        """
        if days < 1:
            days = 1
        if days > 365:
            days = 365
        return hive.budgeter.forecast_spend(days=days).to_dict()

    @app.get("/budget/warning", dependencies=[Depends(require_token)])
    async def budget_warning() -> dict:
        """Return a warning dict when budget health needs attention, or null if healthy."""
        return {"warning": hive.budgeter.warning_status()}

    @app.get("/system-status", dependencies=[Depends(require_token)])
    async def system_status() -> dict:
        """Full system status: router config, budget forecast, memory, tasks, tools."""
        return hive.system_status()

    @app.get("/config/validate", dependencies=[Depends(require_token)])
    async def config_validate() -> dict:
        issues = hive.config.validate()
        return {"valid": len(issues) == 0, "issues": issues}

    @app.get("/config/summary", dependencies=[Depends(require_token)])
    async def config_summary() -> dict:
        """Return the active configuration with secrets redacted."""
        return hive.config.to_safe_dict()

    @app.get("/config/llm", dependencies=[Depends(require_token)])
    async def config_llm() -> dict:
        """Return the LLM model configuration (no secrets)."""
        return hive.config.llm_summary()

    @app.get("/tools", dependencies=[Depends(require_token)])
    async def tools_list() -> dict:
        return {
            "count": len(hive.tools),
            "tools": [
                {"name": t.spec.name, "category": t.spec.category,
                 "description": t.spec.description, "dangerous": t.spec.dangerous,
                 "available": t.available()}
                for t in hive.tools.values()
            ],
        }

    @app.get("/tools/dangerous", dependencies=[Depends(require_token)])
    async def tools_dangerous() -> dict:
        """Return names of all dangerous tools registered in the executor."""
        names = hive.tool_executor.dangerous_tools()
        return {"tools": names, "count": len(names)}

    @app.get("/tools/categories", dependencies=[Depends(require_token)])
    async def tools_categories() -> dict:
        """Return distinct tool categories registered in the executor."""
        cats = hive.tool_executor.tool_categories()
        return {"categories": cats, "count": len(cats)}

    @app.get("/tools/stats", dependencies=[Depends(require_token)])
    async def tools_stats() -> dict:
        return hive.tool_executor.stats()

    @app.post("/memory/{session_id}/consolidate", dependencies=[Depends(require_token)])
    async def memory_consolidate(session_id: str) -> dict:
        """Run the memory-keeper consolidation for a session (aux-model call).
        Extracts durable learnings from recent episodic turns and saves them to memory."""
        count = await hive.consolidate(session_id)
        return {"session_id": session_id, "new_items": count,
                "last_ts": hive.keeper.last_consolidated_ts}

    @app.get("/memory/session/{session_id}/count", dependencies=[Depends(require_token)])
    async def memory_session_count(session_id: str) -> dict:
        """Return the number of episodic turns stored for a session."""
        count = 0
        if hasattr(hive.memory, "count_episodic"):
            count = hive.memory.count_episodic(session_id)
        return {"session_id": session_id, "episodic_count": count}

    @app.delete("/memory/session/{session_id}", dependencies=[Depends(require_token)])
    async def memory_session_delete(session_id: str) -> dict:
        """Delete all episodic memory turns for a session."""
        deleted = 0
        if hasattr(hive.memory, "delete_session_memory"):
            deleted = hive.memory.delete_session_memory(session_id)
        return {"session_id": session_id, "deleted": deleted}

    @app.get("/memory/topics", dependencies=[Depends(require_token)])
    async def memory_topics(kind: str | None = None) -> dict:
        """Return all knowledge topics stored in memory, optionally filtered by kind."""
        if hasattr(hive.memory, "list_topics"):
            topics = hive.memory.list_topics(kind=kind)
        else:
            topics = []
        return {"topics": topics, "count": len(topics), "kind": kind}

    @app.delete("/memory/wipe-knowledge", dependencies=[Depends(require_token)])
    async def memory_wipe_knowledge(kind: str | None = None) -> dict:
        """Delete all knowledge entries, optionally filtered by kind. Returns count deleted."""
        if hasattr(hive.memory, "wipe_knowledge"):
            deleted = hive.memory.wipe_knowledge(kind=kind)
        else:
            deleted = 0
        return {"deleted": deleted, "kind": kind}

    @app.get("/memory/stats", dependencies=[Depends(require_token)])
    async def memory_stats() -> dict:
        """Return memory summary: knowledge count, episodic count, avg importance, timestamps."""
        if hasattr(hive.memory, "memory_stats"):
            return hive.memory.memory_stats()
        return {"knowledge_count": 0, "episodic_count": 0, "avg_importance": 0.0,
                "oldest_ts": None, "newest_ts": None, "by_kind": {}}

    @app.get("/memory/important", dependencies=[Depends(require_token)])
    async def memory_important(limit: int = 10) -> dict:
        """Return the highest-importance knowledge entries."""
        if hasattr(hive.memory, "most_important_facts"):
            facts = hive.memory.most_important_facts(limit=limit)
        else:
            facts = []
        return {"facts": facts, "count": len(facts)}

    @app.get("/memory/export", dependencies=[Depends(require_token)])
    async def memory_export() -> dict:
        if hasattr(hive.memory, "export_backup"):
            return hive.memory.export_backup()
        return {"knowledge": [], "episodic": [],
                "knowledge_count": 0, "episodic_count": 0,
                "note": "export not supported by this memory provider"}

    @app.get("/telemetry", dependencies=[Depends(require_token)])
    async def telemetry() -> dict:
        return hive.telemetry.snapshot()

    @app.get("/traces", dependencies=[Depends(require_token)])
    async def traces_list() -> dict:
        return {"sessions": hive.traces.sessions()}

    @app.get("/traces/stats", dependencies=[Depends(require_token)])
    async def traces_stats() -> dict:
        """Return aggregate stats across all trace sessions."""
        return {
            "session_count": hive.traces.session_count(),
            "total_events": hive.traces.total_event_count(),
            "sessions": hive.traces.sessions(),
        }

    @app.get("/traces/{session_id}", dependencies=[Depends(require_token)])
    async def traces(session_id: str = "default") -> dict:
        return {"session_id": session_id, "events": hive.traces.export(session_id),
                "event_count": hive.traces.event_count(session_id),
                "sessions": hive.traces.sessions()}

    @app.delete("/traces/{session_id}", dependencies=[Depends(require_token)])
    async def traces_clear(session_id: str) -> dict:
        count = hive.traces.clear(session_id)
        return {"cleared": count, "session_id": session_id}

    @app.get("/audit", dependencies=[Depends(require_token)])
    async def audit(limit: int = 50) -> dict:
        return {"entries": hive.audit_log.recent(limit=min(limit, 200))}

    @app.get("/audit/stats", dependencies=[Depends(require_token)])
    async def audit_stats() -> dict:
        """Return audit summary grouped by tool and status."""
        return hive.audit_log.stats()

    @app.get("/audit/search", dependencies=[Depends(require_token)])
    async def audit_search(tool: str | None = None, status: str | None = None,
                           limit: int = 50) -> dict:
        """Search audit log by tool name and/or status."""
        entries = hive.audit_log.search(tool=tool, status=status,
                                        limit=min(limit, 200))
        return {"entries": entries, "count": len(entries)}

    @app.get("/skills/unused", dependencies=[Depends(require_token)])
    async def skills_unused() -> dict:
        """Return active skills that have never been used (use_count == 0)."""
        skills = hive.skill_usage.unused_skills()
        return {"skills": [{"name": s.name, "state": s.state, "agent_created": s.agent_created}
                            for s in skills],
                "count": len(skills)}

    @app.get("/skills/archived", dependencies=[Depends(require_token)])
    async def skills_archived() -> dict:
        """Return all archived skills and their count."""
        archived = hive.skill_usage.by_state("archived")
        return {"skills": [{"name": s.name, "use_count": s.use_count,
                             "archived_ts": s.archived_ts} for s in archived],
                "count": len(archived)}

    @app.get("/skills/recent", dependencies=[Depends(require_token)])
    async def skills_recent(limit: int = 10) -> dict:
        """Return skills ordered by most recently used."""
        skills = hive.skill_usage.recently_used(limit=min(limit, 100))
        return {"skills": [{"name": s.name, "use_count": s.use_count,
                             "last_used_ts": s.last_used_ts, "state": s.state}
                            for s in skills],
                "count": len(skills)}

    # --- Learned skills (SPRINT_7 PILLAR 3) -----------------------------
    # NOTE: registered BEFORE /skills/{name} so the more-specific paths win
    # (FastAPI matches in declaration order; the {name} catchall would otherwise
    # swallow /skills/learned).

    @app.get("/skills/learned", dependencies=[Depends(require_token)])
    async def learned_skills_list(status: str | None = None) -> dict:
        """List learned-skill templates, optionally filtered by lifecycle status."""
        from hive.tools.learned_skills import ALL_STATUSES
        if status is not None and status not in ALL_STATUSES:
            raise HTTPException(status_code=400,
                                detail=f"invalid status {status!r}; valid: {ALL_STATUSES}")
        templates = hive.learned_skills.list_by_status(status)
        return {"templates": [t.to_dict() for t in templates],
                "count": len(templates),
                "stats": hive.learned_skills.stats()}

    @app.get("/skills/learned/{template_id}", dependencies=[Depends(require_token)])
    async def learned_skill_detail(template_id: str) -> dict:
        """Return a single learned-skill template by id."""
        template = hive.learned_skills.get(template_id)
        if template is None:
            raise HTTPException(status_code=404, detail="template not found")
        return template.to_dict()

    @app.post("/skills/learned/propose", dependencies=[Depends(require_token)])
    async def learned_skill_propose(body: dict) -> dict:
        """Propose a learned skill from a tool-call pattern (PILLAR 3).

        Body shape: ``{"pattern": ["tool_a", "tool_b", ...], "description": str?,
        "extra_params": dict?, "force": bool?}``. The Batch B pre-flight smoke
        test runs against the live registry before the template is persisted;
        failures land in ``smoke_failed`` unless ``force=true`` overrides.
        """
        from hive.tools.learned_skills import propose_skill
        pattern = body.get("pattern") or []
        if not isinstance(pattern, list) or not pattern:
            raise HTTPException(status_code=422,
                                detail="'pattern' must be a non-empty list of tool names")
        pattern = [str(p) for p in pattern]
        try:
            template = propose_skill(
                pattern,
                description=str(body.get("description", "")),
                extra_params=body.get("extra_params"),
                seq_id=str(body.get("seq_id", "")) or None,
                registry=hive.tools,
                force=bool(body.get("force", False)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        hive.learned_skills.save(template)
        return template.to_dict()

    @app.post("/skills/learned/{template_id}/approve", dependencies=[Depends(require_token)])
    async def learned_skill_approve(template_id: str) -> dict:
        """Approve a proposed template and register it as a live tool.

        Idempotent: re-approving a registered template is a no-op (returns
        current state). Registration is gated on the template's id not already
        being a registered tool name."""
        from hive.tools.learned_skills import (
            STATUS_APPROVED,
            STATUS_REGISTERED,
            add_learned_skill,
        )
        template = hive.learned_skills.get(template_id)
        if template is None:
            raise HTTPException(status_code=404, detail="template not found")
        if template.status == STATUS_REGISTERED:
            return template.to_dict()
        template.status = STATUS_APPROVED
        out = add_learned_skill(
            template,
            registry=hive.tools,
            skill_usage=hive.skill_usage,
            store=hive.learned_skills,
            auto_approve=True,
            executor=hive.tool_executor,
        )
        return out.to_dict()

    @app.post("/skills/learned/{template_id}/reject", dependencies=[Depends(require_token)])
    async def learned_skill_reject(template_id: str) -> dict:
        """Reject a proposed template (does not delete; just marks it rejected)."""
        from hive.tools.learned_skills import STATUS_REJECTED
        template = hive.learned_skills.get(template_id)
        if template is None:
            raise HTTPException(status_code=404, detail="template not found")
        hive.learned_skills.update_status(template_id, STATUS_REJECTED)
        out = hive.learned_skills.get(template_id)
        return out.to_dict() if out else {}

    @app.post("/skills/learned/detect", dependencies=[Depends(require_token)])
    async def learned_skill_detect(body: dict | None = None) -> dict:
        """Detect repeated tool-call sequences in the recent audit log.

        Body (all optional): ``{"limit": int, "min_repeats": int,
        "min_seq_len": int, "max_seq_len": int}``. Returns the top patterns by
        occurrence count. Useful for an operator to see what Hive would learn."""
        from hive.tools.learned_skills import detect_patterns
        body = body or {}
        try:
            limit = max(1, min(100, int(body.get("limit", 20))))
            min_repeats = max(2, int(body.get("min_repeats", 2)))
            min_seq_len = max(2, min(10, int(body.get("min_seq_len", 3))))
            max_seq_len = max(min_seq_len, min(10, int(body.get("max_seq_len", 5))))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="numeric fields invalid")
        entries = hive.audit_log.export()
        patterns = detect_patterns(
            entries,
            min_repeats=min_repeats,
            min_seq_len=min_seq_len,
            max_seq_len=max_seq_len,
            limit=limit,
        )
        return {"patterns": [{"sequence": list(seq), "count": c}
                              for seq, c in patterns],
                "scanned_entries": len(entries)}

    @app.post("/skills/{name}/pin", dependencies=[Depends(require_token)])
    async def skill_pin(name: str) -> dict:
        """Pin a skill (prevents archiving)."""
        ok = hive.skill_usage.pin(name)
        if not ok:
            raise HTTPException(status_code=404, detail="skill not found")
        return {"pinned": True, "name": name}

    @app.post("/skills/{name}/unpin", dependencies=[Depends(require_token)])
    async def skill_unpin(name: str) -> dict:
        """Remove the pin from a skill."""
        ok = hive.skill_usage.unpin(name)
        if not ok:
            raise HTTPException(status_code=404, detail="skill not found")
        return {"pinned": False, "name": name}

    @app.get("/skills/{name}", dependencies=[Depends(require_token)])
    async def skill_detail(name: str) -> dict:
        """Return detail for a single skill by name."""
        skill = hive.skill_usage.get(name)
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        return {"name": skill.name, "use_count": skill.use_count,
                "last_used_ts": skill.last_used_ts, "state": skill.state,
                "pinned": skill.pinned, "agent_created": skill.agent_created}

    @app.post("/skills/{name}/state", dependencies=[Depends(require_token)])
    async def skill_set_state(name: str, body: dict) -> dict:
        """Set lifecycle state for a skill (P-I review: was 404'ing in MissionControl).
        Body: {"state": "active"|"stale"|"archived"}.
        Returns {"name", "state", "archived_ts"}.

        Routed through the Curator (not skill_usage.set_state directly) so a
        manual archive/restore here gets the same live-registry side effects
        as the automatic curator.run() lifecycle: archiving deregisters the
        skill from the live tool registry, un-archiving re-registers it."""
        from hive.memory.skill_usage import (
            STATE_ACTIVE,
            STATE_ARCHIVED,
            STATE_STALE,
        )
        if not isinstance(body, dict) or "state" not in body:
            raise HTTPException(status_code=400, detail="missing 'state' field")
        state = body["state"]
        if state not in (STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED):
            raise HTTPException(status_code=400,
                                detail=f"invalid state {state!r}")
        ok = hive.curator.set_state(name, state)
        if not ok:
            raise HTTPException(status_code=404, detail="skill not found")
        skill = hive.skill_usage.get(name)
        archived_ts = skill.archived_ts if skill else None
        return {"name": name, "state": state, "archived_ts": archived_ts}

    @app.get("/skills", dependencies=[Depends(require_token)])
    async def skills_list(pinned: bool = False) -> dict:
        """Return skill usage statistics, or pinned names when ?pinned=true (P-I T2.3)."""
        if pinned:
            return {"pinned": hive.skill_usage.pinned_names()}
        return hive.skill_usage.stats()

    @app.get("/audit/error-rate", dependencies=[Depends(require_token)])
    async def audit_error_rate(window_hours: float = 24.0) -> dict:
        """Return the fraction of tool calls that errored in a time window."""
        rate = hive.audit_log.error_rate(window_hours=window_hours)
        return {"error_rate": rate, "window_hours": window_hours}

    @app.get("/audit/errors", dependencies=[Depends(require_token)])
    async def audit_errors(limit: int = 20) -> dict:
        """Return the most recent failed/error audit entries."""
        entries = hive.audit_log.recent_errors(limit=min(limit, 200))
        return {"entries": entries, "count": len(entries)}

    @app.get("/audit/recent/{tool}", dependencies=[Depends(require_token)])
    async def audit_recent_by_tool(tool: str, limit: int = 20) -> dict:
        """Return the most recent audit entries for a specific tool."""
        entries = hive.audit_log.recent_by_tool(tool, limit=min(limit, 200))
        return {"tool": tool, "entries": entries, "count": len(entries)}

    @app.delete("/audit/purge", dependencies=[Depends(require_token)])
    async def audit_purge(max_age_days: float = 90.0) -> dict:
        """Delete audit entries older than max_age_days. Returns count purged."""
        deleted = hive.audit_log.purge_old(max_age_days=max_age_days)
        return {"deleted": deleted, "max_age_days": max_age_days}

    @app.get("/audit/export", dependencies=[Depends(require_token)])
    async def audit_export(start_ts: float | None = None,
                           end_ts: float | None = None) -> dict:
        """Export audit entries for a time range (UNIX timestamps). Omit params for all."""
        entries = hive.audit_log.export(start_ts=start_ts, end_ts=end_ts)
        return {"entries": entries, "count": len(entries)}

    @app.get("/tasks", dependencies=[Depends(require_token)])
    async def tasks(kind: str | None = None, source: str | None = None,
                    state: str | None = None) -> dict:
        if kind is not None or source is not None or state is not None:
            found = hive.task_board.search(kind=kind, source=source, state=state)
            return {
                "pending": hive.task_board.pending_count(),
                "tasks": [
                    {"id": t.id, "kind": t.kind, "state": t.state,
                     "source": t.source, "attempts": t.attempts,
                     "last_error": t.last_error, "created_ts": t.created_ts,
                     "payload": t.payload}
                    for t in found
                ],
            }
        recent = hive.task_board.all()[-20:]  # last 20 across all states
        return {
            "pending": hive.task_board.pending_count(),
            "tasks": [
                {"id": t.id, "kind": t.kind, "state": t.state,
                 "source": t.source, "attempts": t.attempts,
                 "last_error": t.last_error, "created_ts": t.created_ts,
                 "payload": t.payload}
                for t in reversed(recent)  # newest first
            ],
        }

    @app.get("/tasks/by-kind", dependencies=[Depends(require_token)])
    async def tasks_by_kind() -> dict:
        return {"by_kind": hive.task_board.count_by_kind()}

    @app.get("/tasks/stats", dependencies=[Depends(require_token)])
    async def tasks_stats() -> dict:
        return hive.task_board.statistics()

    @app.get("/tasks/last-failed", dependencies=[Depends(require_token)])
    async def task_last_failed() -> dict:
        """Return the single most recently failed task, or null."""
        task = hive.task_board.last_failed()
        if task is None:
            return {"task": None}
        return {"task": {"id": task.id, "kind": task.kind, "source": task.source,
                          "attempts": task.attempts, "last_error": task.last_error,
                          "updated_ts": task.updated_ts}}

    @app.get("/tasks/failed", dependencies=[Depends(require_token)])
    async def tasks_failed(limit: int = 10) -> dict:
        """Return the most recently failed tasks."""
        items = hive.task_board.recent_failures(limit=min(limit, 100))
        return {"tasks": [{"id": t.id, "kind": t.kind, "source": t.source,
                           "attempts": t.attempts, "last_error": t.last_error,
                           "updated_ts": t.updated_ts} for t in items]}

    @app.get("/tasks/running", dependencies=[Depends(require_token)])
    async def tasks_running() -> dict:
        """Return all currently RUNNING tasks."""
        running = hive.task_board.all(state="running")
        return {"tasks": [{"id": t.id, "kind": t.kind, "source": t.source,
                            "attempts": t.attempts, "created_ts": t.created_ts}
                           for t in running],
                "count": len(running)}

    @app.post("/tasks/retry-failed", dependencies=[Depends(require_token)])
    async def tasks_retry_failed() -> dict:
        """Bulk-retry all failed tasks, resetting them to pending."""
        count = hive.task_board.retry_all_failed()
        return {"retried": count}

    @app.post("/tasks/bulk-cancel", dependencies=[Depends(require_token)])
    async def tasks_bulk_cancel(body: dict | None = None) -> dict:
        """Cancel all PENDING tasks, optionally filtered by kind."""
        kind = (body or {}).get("kind")
        count = hive.task_board.bulk_cancel_pending(kind=kind or None)
        return {"cancelled": count, "kind": kind}

    @app.post("/tasks/requeue-running", dependencies=[Depends(require_token)])
    async def tasks_requeue_running() -> dict:
        """Reset all RUNNING tasks back to PENDING (crash-recovery after unclean shutdown)."""
        return hive.resume_after_restart()

    @app.get("/tasks/{task_id}", dependencies=[Depends(require_token)])
    async def task_get(task_id: int) -> dict:
        task = hive.task_board.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return {"id": task.id, "kind": task.kind, "state": task.state,
                "source": task.source, "attempts": task.attempts,
                "last_error": task.last_error, "created_ts": task.created_ts,
                "payload": task.payload}

    @app.post("/tasks/{task_id}/retry", dependencies=[Depends(require_token)])
    async def task_retry(task_id: int) -> dict:
        ok = hive.task_board.retry(task_id)
        if not ok:
            raise HTTPException(status_code=409, detail="task is not in failed state")
        return {"retried": True, "task_id": task_id}

    @app.post("/tasks/{task_id}/cancel", dependencies=[Depends(require_token)])
    async def task_cancel(task_id: int) -> dict:
        ok = hive.task_board.cancel(task_id)
        if not ok:
            raise HTTPException(status_code=409, detail="task is not in pending state")
        return {"cancelled": True, "task_id": task_id}

    @app.get("/sessions", dependencies=[Depends(require_token)])
    async def sessions_list() -> dict:
        return {"sessions": hive.session_store.list_sessions()}

    @app.get("/sessions/stats", dependencies=[Depends(require_token)])
    async def sessions_stats() -> dict:
        return hive.session_store.stats()

    @app.get("/sessions/search", dependencies=[Depends(require_token)])
    async def sessions_search(q: str, session_id: str | None = None,
                              limit: int = 10) -> dict:
        results = hive.session_store.search(q, session_id=session_id,
                                            limit=min(limit, 100))
        return {"results": results, "count": len(results)}

    @app.get("/sessions/{session_id}", dependencies=[Depends(require_token)])
    async def session_get(session_id: str) -> dict:
        msg_count = hive.session_store.count_messages(session_id)
        title = hive.session_store.get_title(session_id)
        summary = hive.session_store.get_summary(session_id)
        return {"session_id": session_id, "message_count": msg_count,
                "title": title, "summary": summary}

    @app.get("/sessions/{session_id}/title", dependencies=[Depends(require_token)])
    async def session_get_title(session_id: str) -> dict:
        title = hive.session_store.get_title(session_id)
        return {"session_id": session_id, "title": title}

    @app.post("/sessions/{session_id}/title", dependencies=[Depends(require_token)])
    async def session_set_title(session_id: str, body: dict) -> dict:
        title = body.get("title", "")
        if not title:
            raise HTTPException(status_code=422, detail="title is required")
        hive.session_store.ensure(session_id)
        hive.session_store.set_title(session_id, title)
        return {"session_id": session_id, "title": title}

    @app.post("/sessions/{session_id}/auto-title", dependencies=[Depends(require_token)])
    async def session_auto_title(session_id: str) -> dict:
        """Generate a short title for the session from its first message (best-effort)."""
        title = await hive.title_session(session_id)
        return {"session_id": session_id, "title": title}

    @app.delete("/sessions/{session_id}", dependencies=[Depends(require_token)])
    async def session_delete(session_id: str) -> dict:
        deleted = hive.session_store.delete_session(session_id)
        return {"deleted": deleted, "session_id": session_id}

    @app.get("/cron/stats", dependencies=[Depends(require_token)])
    async def cron_stats() -> dict:
        """Return cron job counts: total, enabled, and due-now."""
        all_jobs = hive.cron.jobs()
        return {
            "total": len(all_jobs),
            "enabled": hive.cron.enabled_count(),
            "due_now": hive.cron.due_count(),
        }

    @app.get("/cron", dependencies=[Depends(require_token)])
    async def cron_list() -> dict:
        jobs = hive.cron.jobs()
        return {"jobs": [
            {"id": j.id, "schedule": j.schedule, "task_kind": j.task_kind,
             "payload": j.payload, "enabled": j.enabled,
             "last_run": j.last_run, "next_run": j.next_run}
            for j in jobs
        ]}

    @app.post("/cron", dependencies=[Depends(require_token)])
    async def cron_add(body: dict) -> dict:
        schedule = body.get("schedule", "")
        task_kind = body.get("task_kind", "")
        if not schedule or not task_kind:
            raise HTTPException(status_code=422, detail="schedule and task_kind are required")
        job_id = hive.cron.add(schedule, task_kind, body.get("payload"),
                               enabled=body.get("enabled", True))
        return {"id": job_id, "schedule": schedule, "task_kind": task_kind}

    @app.post("/cron/{job_id}/enable", dependencies=[Depends(require_token)])
    async def cron_enable(job_id: int) -> dict:
        hive.cron.set_enabled(job_id, True)
        return {"enabled": True, "job_id": job_id}

    @app.post("/cron/{job_id}/disable", dependencies=[Depends(require_token)])
    async def cron_disable(job_id: int) -> dict:
        hive.cron.set_enabled(job_id, False)
        return {"enabled": False, "job_id": job_id}

    @app.delete("/cron/{job_id}", dependencies=[Depends(require_token)])
    async def cron_remove(job_id: int) -> dict:
        ok = hive.cron.remove(job_id)
        if not ok:
            raise HTTPException(status_code=404, detail="cron job not found")
        return {"removed": True, "job_id": job_id}

    @app.get("/cron/{job_id}", dependencies=[Depends(require_token)])
    async def cron_get(job_id: int) -> dict:
        job = hive.cron.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="cron job not found")
        return {"id": job.id, "schedule": job.schedule, "task_kind": job.task_kind,
                "payload": job.payload, "enabled": job.enabled,
                "last_run": job.last_run, "next_run": job.next_run}

    @app.get("/commitments", dependencies=[Depends(require_token)])
    async def commitments_list(active_only: bool = False) -> dict:
        items = hive.commitments.all(active_only=active_only)
        return {"commitments": [
            {"id": c.id, "description": c.description,
             "cadence_seconds": c.cadence_seconds, "task_kind": c.task_kind,
             "active": c.active, "last_fulfilled": c.last_fulfilled,
             "created_ts": c.created_ts}
            for c in items
        ]}

    @app.post("/commitments", dependencies=[Depends(require_token)])
    async def commitments_add(body: dict) -> dict:
        description = body.get("description", "")
        cadence = body.get("cadence_seconds")
        if not description or cadence is None:
            raise HTTPException(status_code=422,
                                detail="description and cadence_seconds are required")
        cid = hive.commitments.add(
            description, float(cadence),
            task_kind=body.get("task_kind", "commitment"),
            payload=body.get("payload"),
        )
        return {"id": cid, "description": description, "cadence_seconds": cadence}

    @app.delete("/commitments/{commitment_id}", dependencies=[Depends(require_token)])
    async def commitments_remove(commitment_id: int) -> dict:
        ok = hive.commitments.remove(commitment_id)
        if not ok:
            raise HTTPException(status_code=404, detail="commitment not found")
        return {"removed": True, "commitment_id": commitment_id}

    @app.post("/commitments/{commitment_id}/fulfill", dependencies=[Depends(require_token)])
    async def commitments_fulfill(commitment_id: int) -> dict:
        ok = hive.commitments.fulfill(commitment_id)
        if not ok:
            raise HTTPException(status_code=404, detail="commitment not found")
        return {"fulfilled": True, "commitment_id": commitment_id}

    @app.get("/commitments/overdue", dependencies=[Depends(require_token)])
    async def commitments_overdue() -> dict:
        items = hive.commitments.overdue()
        return {"overdue": [
            {"id": c.id, "description": c.description,
             "cadence_seconds": c.cadence_seconds, "last_fulfilled": c.last_fulfilled}
            for c in items
        ]}

    @app.get("/commitments/upcoming", dependencies=[Depends(require_token)])
    async def commitments_upcoming(limit: int = 5) -> dict:
        """Return the next N active commitments sorted by when they will be due (soonest first)."""
        items = hive.commitments.upcoming(limit=min(limit, 50))
        return {"upcoming": [
            {"id": c.id, "description": c.description,
             "cadence_seconds": c.cadence_seconds, "last_fulfilled": c.last_fulfilled}
            for c in items
        ], "count": len(items)}

    @app.get("/approvals/edits", dependencies=[Depends(require_token)])
    async def approvals_edits() -> dict:
        """List all pending REVIEW-tier self-mod edits awaiting human decision."""
        return {"pending_edits": hive.pending_review_edits(),
                "count": hive.improver.pending_count()}

    @app.delete("/approvals/cancel-all", dependencies=[Depends(require_token)])
    async def approvals_cancel_all() -> dict:
        """Cancel ALL pending REVIEW-tier self-mod edits at once."""
        count = hive.abort_all_self_mods()
        return {"cancelled": count}

    @app.get("/llm/pool", dependencies=[Depends(require_token)])
    async def llm_pool_status() -> dict:
        """Return the credential pool status (labels, available count, failure counts)."""
        pool = getattr(hive.router, "_pool", None)
        if pool is None:
            return {"pool_size": 0, "available": 0, "labels": [], "total_failures": 0,
                    "failure_counts": {}}
        return {
            "pool_size": len(pool),
            "available": pool.available_count(),
            "labels": pool.labels(),
            "failure_counts": pool.failure_counts(),
            "total_failures": pool.total_failures(),
        }

    @app.get("/model/catalog", dependencies=[Depends(require_token)])
    async def model_catalog() -> dict:
        """List all registered model IDs in the model catalog."""
        catalog = getattr(hive.router, "_catalog", None)
        if catalog is None:
            return {"models": [], "count": 0}
        return {"models": catalog.list_models(), "count": len(catalog)}

    @app.post("/run-tests", dependencies=[Depends(require_token)])
    async def run_tests_endpoint(dry_run: bool = False) -> dict:
        """Run the project test suite and return structured results.
        Safe to call at any time — never triggers self-modification.
        Use POST /self-diagnose to run tests AND trigger improvements."""
        if dry_run:
            return {"all_passed": True, "passed": 0, "failed": 0, "errors": 0,
                    "skipped": 0, "timed_out": False, "dry_run": True, "output": ""}
        return await hive.run_tests()

    @app.get("/traces/export/{session_id}", dependencies=[Depends(require_token)])
    async def traces_export(session_id: str) -> dict:
        """Export a session's event trace as a structured, JSON-serialisable list."""
        events = hive.traces.export(session_id)
        return {"session_id": session_id, "events": events, "count": len(events)}

    @app.get("/self-improve/history", dependencies=[Depends(require_token)])
    async def self_improve_history(limit: int = 20) -> dict:
        """Return the most recent self-mod proposal outcomes (newest first)."""
        records = hive.self_modifier.history(limit=max(1, min(limit, 100)))
        return {"history": records, "count": len(records)}

    @app.get("/self-improve/stages", dependencies=[Depends(require_token)])
    async def self_improve_stages() -> dict:
        """Return proposal counts grouped by terminal stage (test/protected/pushed/etc.)."""
        by_stage = hive.self_modifier.proposals_by_stage()
        return {"by_stage": by_stage, "total": sum(by_stage.values())}

    @app.get("/self-improve/status", dependencies=[Depends(require_token)])
    async def self_improve_status() -> dict:
        """Comprehensive self-improvement system status snapshot."""
        return {
            "pending_review_count": hive.improver.pending_count(),
            "pending_review": hive.improver.describe_pending(),
            "recent_branches": hive.self_modifier.recent_branches(n=5),
            "last_result": hive.self_modifier.last_result,
            "history_count": len(hive.self_modifier.history(limit=1000)),
        }

    @app.get("/self-improve/pending", dependencies=[Depends(require_token)])
    async def self_improve_pending() -> dict:
        """Return detailed metadata for all pending REVIEW-tier edits."""
        pending = hive.improver.describe_pending()
        return {"pending": pending, "count": len(pending)}

    @app.post("/self-improve/symptom", dependencies=[Depends(require_token)])
    async def self_improve_symptom(body: dict) -> dict:
        """Trigger a symptom-based self-improvement cycle without running tests first.
        The LLM diagnoser analyses the symptom and proposes typed edits; AUTO tier
        opens a draft PR, REVIEW tier queues for human approval."""
        symptom = body.get("symptom", "").strip()
        if not symptom:
            raise HTTPException(status_code=422, detail="symptom is required")
        outcomes = await hive.self_improve_from_symptom(symptom)
        return {"outcomes": [
            {"status": o.status, "op": o.op.value, "tier": o.tier.value,
             "detail": o.detail, "branch": o.branch, "approval_id": o.approval_id}
            for o in outcomes
        ]}

    @app.post("/self-diagnose", dependencies=[Depends(require_token)])
    async def self_diagnose_endpoint(dry_run: bool = False) -> dict:
        """Run the test suite and trigger a self-improvement cycle for any failures.
        Hive never auto-merges: AUTO tier edits open draft PRs, REVIEW tier goes to
        /approvals for human decision. Safe to call at any time."""
        return await hive.self_diagnose(dry_run=dry_run)

    @app.get("/approvals", dependencies=[Depends(require_token)])
    async def approvals() -> dict:
        return {"pending": gate.pending(),
                "pending_edits": hive.improver.pending_count()}

    @app.post("/approvals/cancel", dependencies=[Depends(require_token)])
    async def approvals_cancel(body: ApprovalDecision) -> dict:
        """Cancel a pending REVIEW-tier self-mod edit without applying it."""
        removed = hive.improver.cancel_review(body.approval_id)
        if not removed:
            raise HTTPException(status_code=404, detail="pending edit not found")
        hive.edit_pending.pop(body.approval_id, None)
        return {"cancelled": True, "approval_id": body.approval_id}

    @app.post("/approvals/decide", dependencies=[Depends(require_approver)])
    async def decide(body: ApprovalDecision) -> dict:
        # Route through the enhancements layer: records an AuditRecord, honors
        # the kill-switch, and expires stale pending items before delegating to
        # the PROTECTED gate.
        item, outcome = enhance.resolve_with_outcome(
            body.approval_id, body.approved, decided_by="human:web",
        )
        if item is None or outcome is None:
            raise HTTPException(status_code=404, detail="unknown approval")
        if outcome is not DecisionOutcome.APPROVED:
            hive.edit_pending.pop(body.approval_id, None)
            hive.task_board.resolve_approval(
                body.approval_id, approved=False,
                error=f"approval {outcome.value}",
            )
            return {"executed": False, "status": outcome.value}
        # Self-mod REVIEW-tier edit: route to the self-modifier, not the tool executor.
        if str(item.get("tool", "")).startswith("self_mod:"):
            edit = hive.edit_pending.pop(body.approval_id, None)
            if edit is None:
                return {"executed": False,
                        "error": "edit not found (process may have restarted)"}
            outcome = await hive.improver.apply_approved(edit)
            return {"executed": True, "status": outcome.status,
                    "branch": outcome.branch, "detail": outcome.detail}
        dispatch = await hive.tool_executor.execute_approved(item["tool"], item["args"])
        hive.task_board.resolve_approval(
            body.approval_id,
            approved=dispatch.status is DispatchStatus.OK,
            error=dispatch.error or "approved tool did not complete",
        )
        return {"executed": True, "status": dispatch.status.value,
                "result": dispatch.result.content if dispatch.result else None,
                "error": dispatch.error}

    # ------------------------------------------------------------------
    # Approval Gate hardening — expiry, kill-switch, history (Pillar 2)
    # ------------------------------------------------------------------

    @app.post("/approvals/expire", dependencies=[Depends(require_token)])
    async def approvals_expire() -> dict:
        """Sweep all pending approvals past TTL and force-reject them.

        Returns the list of expired approval ids. Idempotent.
        """
        expired = enhance.sweep_expired()
        for approval_id in expired:
            hive.edit_pending.pop(approval_id, None)
            hive.task_board.resolve_approval(
                approval_id, approved=False, error="approval expired")
        return {"expired": expired, "count": len(expired)}
    @app.get("/approvals/emergency-stop", dependencies=[Depends(require_token)])
    async def approvals_emergency_stop_get() -> dict:
        """Return the current kill-switch state (active flag, who/when)."""
        return enhance.kill_state()

    @app.post("/approvals/emergency-stop", dependencies=[Depends(require_token)])
    async def approvals_emergency_stop_engage(body: dict | None = None) -> dict:
        """Engage (or release) the global emergency stop.

        Body: ``{"action": "engage"|"release", "engaged_by": "...", "note": "..."}``.
        When engaged, every pending approval is force-terminated as KILLED and
        no new approval requests are accepted until release.
        """
        body = body or {}
        action = str(body.get("action", "engage")).lower()
        if action == "release":
            return enhance.release_kill_switch(
                released_by=str(body.get("released_by", "operator:web")))
        result = enhance.engage_kill_switch(
            engaged_by=str(body.get("engaged_by", "operator:web")),
            note=str(body.get("note", "")),
        )
        for approval_id in result.pop("killed_ids", []):
            hive.edit_pending.pop(approval_id, None)
            hive.task_board.resolve_approval(
                approval_id, approved=False, error="approval killed by emergency stop")
        return result

    @app.get("/approvals/history", dependencies=[Depends(require_token)])
    async def approvals_history(limit: int = 50, tool: str | None = None,
                                outcome: str | None = None,
                                since: float | None = None) -> dict:
        """Return the structured decision audit trail (newest first).

        Filterable by ``tool`, `outcome` (approved|rejected|expired|killed),
        and ``since`` (UNIX timestamp).
        """
        parsed_outcome = None
        if outcome:
            try:
                parsed_outcome = DecisionOutcome(outcome)
            except ValueError:
                raise HTTPException(status_code=400,
                                    detail=f"invalid outcome {outcome!r}")
        records = enhance.history(limit=limit, tool=tool,
                                  outcome=parsed_outcome, since=since)
        return {
            "count": len(records),
            "records": [r.to_dict() for r in records],
            "stats": enhance.history_stats(),
        }

    @app.get("/events/history", dependencies=[Depends(require_token)])
    async def events_history(n: int = 20) -> dict:
        """Return the n most recent EventBus events (newest first)."""
        events_list = hive.events.recent_events(n=max(1, min(n, 500)))
        return {"events": events_list, "count": len(events_list)}

    @app.get("/events/stats", dependencies=[Depends(require_token)])
    async def events_stats() -> dict:
        """Return event count totals by type from the EventBus history."""
        return {"by_type": hive.events.history_by_type(),
                "total": hive.events.history_count(),
                "subscribers": hive.events.total_subscribers()}

    @app.get("/loop-guard/stats", dependencies=[Depends(require_token)])
    async def loop_guard_stats() -> dict:
        """Return current LoopGuard statistics."""
        return hive.loop_guard_stats()

    @app.post("/loop-guard/reset", dependencies=[Depends(require_token)])
    async def loop_guard_reset() -> dict:
        """Reset the LoopGuard call history and per-tool counters."""
        hive.reset_loop_guard()
        return {"reset": True}

    @app.get("/loop-guard/top-tools", dependencies=[Depends(require_token)])
    async def loop_guard_top_tools(n: int = 5) -> dict:
        """Return the n most-called tools in this guard window, by call count."""
        top = hive.loop_guard.top_repeated_tools(n=n)
        return {"tools": [{"name": name, "calls": count} for name, count in top]}

    @app.get("/commitments/active", dependencies=[Depends(require_token)])
    async def commitments_active_names() -> dict:
        """Return the descriptions of all active commitments."""
        names = hive.commitments.active_names()
        return {"names": names, "count": len(names)}

    @app.get("/agents/board", dependencies=[Depends(require_token)])
    async def agents_board() -> dict:
        """Snapshot of the multi-agent Kanban board (SPRINT_6 P-G).

        Returns 5 fixed columns keyed by named sub-agent name. Each column is
        a list of cards (oldest first) with status, elapsed time, task, and
        session_id for trace drill-down. Cards older than board TTL are pruned.
        """
        snap = hive.board.snapshot()
        return {"columns": {
            name: [
                {
                    "request_id": c.request_id,
                    "method": c.method,
                    "task": c.task,
                    "status": c.status,
                    "started_at": c.started_at,
                    "finished_at": c.finished_at,
                    "tool_calls": c.tool_calls,
                    "session_id": c.session_id,
                    "result": c.result if c.status == "done" else None,
                    "error": c.error,
                }
                for c in cards
            ]
            for name, cards in snap.items()
        }}

    @app.websocket("/ws/dashboard")
    async def ws_dashboard(websocket: WebSocket) -> None:
        """Real-time dashboard event stream.

        After the token handshake, pushes EventBus events as JSON to the client.
        Replaces the polling approach (telemetry/audit/tasks every N seconds)
        with a single persistent connection that delivers deltas immediately."""
        await websocket.accept()
        token = await websocket.receive_text()
        if not token_ok(token, secret):
            await websocket.send_json({"type": "error", "data": "unauthorized"})
            await websocket.close()
            return

        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=200)

        from hive.core.events import EventType as _ET

        def _on_event(event: object) -> None:
            try:
                payload = {
                    "type": getattr(getattr(event, "event_type", None), "value", "unknown"),
                    "data": getattr(event, "data", {}),
                    "ts": getattr(event, "timestamp", 0),
                }
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # drop when client is slow

        # Subscribe to the event types most useful for the dashboard
        _DASHBOARD_EVENTS = (
            _ET.TOOL_CALL_END, _ET.INFERENCE_END, _ET.AGENT_TICK_END,
            _ET.APPROVAL_REQUESTED, _ET.APPROVAL_RESOLVED,
            _ET.SELFMOD_START, _ET.SELFMOD_END,
            _ET.BUDGET_BLOCK, _ET.MEMORY_STORE,
            _ET.A2A_CALL_STARTED, _ET.A2A_CALL_COMPLETED, _ET.A2A_CALL_FAILED,
        )
        for et in _DASHBOARD_EVENTS:
            hive.events.subscribe(et, _on_event)

        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=30)
                    await websocket.send_json(payload)
                except asyncio.TimeoutError:
                    # Send a keepalive ping so the browser doesn't time out
                    await websocket.send_json({"type": "ping"})
        except WebSocketDisconnect:
            log.info("ws/dashboard client disconnected")
        finally:
            for et in _DASHBOARD_EVENTS:
                with hive.events._lock:
                    subs = hive.events._subs.get(et, [])
                    if _on_event in subs:
                        subs.remove(_on_event)

    @app.websocket("/ws/audit")
    async def ws_audit(websocket: WebSocket) -> None:
        """Real-time audit log stream (SPRINT_7 Batch E).

        Each client receives an initial back-fill (last 20 rows) followed by
        every new audit row as it's recorded. Auth: token-on-open (the first
        text frame must be the shared secret) or the ``X-Hive-Token`` header —
        matches /ws and /ws/dashboard. Deliberately no ``?token=`` query
        parameter: a query string ends up in server/proxy access logs and
        browser history, unlike the header or first-frame paths.

        Heartbeats: a ``{"type": "heartbeat"}`` JSON is emitted every 30s of
        idleness so the connection survives reverse-proxy / browser idle
        timeouts. The client never sees the broadcaster's per-tool rate
        limiting — that's applied before publication.
        """
        await websocket.accept()
        token = websocket.headers.get("x-hive-token")
        if not token:
            # Fall back to the token-on-open pattern (matches /ws and
            # /ws/dashboard — the browser sends the secret as the first
            # text frame).
            try:
                token = await asyncio.wait_for(websocket.receive_text(),
                                                timeout=5.0)
            except (asyncio.TimeoutError, WebSocketDisconnect):
                await websocket.close(code=4401)
                return
        if not token_ok(token, secret):
            await websocket.close(code=4401)
            return

        from hive.observability.audit import _audit_broadcaster
        q = _audit_broadcaster.subscribe()
        try:
            # Initial back-fill: last 20 audit rows.
            initial_count = 0
            for row in hive.audit_log.recent(limit=20):
                try:
                    await websocket.send_json({
                        "type": "audit_history",
                        "entry": row,
                    })
                    initial_count += 1
                except WebSocketDisconnect:
                    return
            # Sentinel so clients can tell "back-fill done" from "no rows"
            # without waiting 30s for the first heartbeat. (Cheap to send.)
            try:
                await websocket.send_json({
                    "type": "audit_ready",
                    "initial_count": initial_count,
                })
            except WebSocketDisconnect:
                return

            # Stream new rows. ``q.get(timeout=30)`` blocks for up to 30s; on
            # timeout it raises ``queue.Empty`` which we convert into a
            # heartbeat so the connection survives reverse-proxy idle
            # timeouts.
            while True:
                try:
                    row = await asyncio.to_thread(q.get, timeout=30)
                except queue.Empty:  # type: ignore[attr-defined]
                    try:
                        await websocket.send_json({"type": "heartbeat"})
                    except WebSocketDisconnect:
                        return
                    continue
                except Exception as exc:  # noqa: BLE001
                    log.warning("ws/audit queue read failed: %s", exc)
                    break
                try:
                    await websocket.send_json({"type": "audit", "entry": row})
                except WebSocketDisconnect:
                    return
        except WebSocketDisconnect:
            log.info("ws/audit client disconnected")
        finally:
            _audit_broadcaster.unsubscribe(q)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        token = await websocket.receive_text()
        if not token_ok(token, secret):
            await websocket.send_json({"type": "error", "data": "unauthorized"})
            await websocket.close()
            return
        ws_session_id = f"ws-{uuid.uuid4().hex[:12]}"
        try:
            while True:
                try:
                    user_msg = await asyncio.wait_for(
                        websocket.receive_text(),
                        timeout=cfg.ws_idle_timeout,
                    )
                except asyncio.TimeoutError:
                    await websocket.send_json({"type": "error", "data": "idle timeout"})
                    await websocket.close()
                    return
                if len(user_msg) > cfg.max_message_len:
                    await websocket.send_json({"type": "error", "data": "message too long"})
                    continue
                try:
                    reply = await hive.ask(user_msg, session_id=ws_session_id, channel_hint="web")
                    await websocket.send_json({"type": "reply", "data": reply})
                except Exception as exc:  # noqa: BLE001
                    log.error("ws turn error: %s", exc, exc_info=True)
                    await websocket.send_json({"type": "error", "data": "internal error"})
        except WebSocketDisconnect:
            log.info("ws client disconnected (session=%s)", ws_session_id)

    # ------------------------------------------------------------------
    # OpenAI-compatible /v1/ endpoints (drop-in for Cursor, Aider, Continue, etc.)
    # ------------------------------------------------------------------

    @app.get("/v1/models", dependencies=[Depends(require_token)])
    async def v1_models() -> dict:
        """OpenAI-compatible model listing."""
        return {
            "object": "list",
            "data": [{"id": "hive", "object": "model", "created": 0,
                      "owned_by": "hiveosagent"}],
        }

    @app.post("/v1/chat/completions", dependencies=[Depends(require_token)], response_model=None)
    async def v1_chat_completions(request: Request):
        """OpenAI-compatible chat completions endpoint.

        Accepts the standard OpenAI request body (model, messages, stream).
        The last user message is routed through the HiveOS orchestrator.
        All prior messages are joined as context if more than one user turn is present.
        """
        import time
        import uuid

        body: dict = await request.json()
        messages: list[dict] = body.get("messages", [])
        stream: bool = bool(body.get("stream", False))
        session_id: str = body.get("session_id", "v1-default")
        # SPRINT_6 P-C: opt-in iteration streaming via custom header. When true
        # AND stream=true, emit OpenAI-shaped chunks that also surface tool
        # activity (delta.tool_calls + marker delta.content). Default off so
        # existing OpenAI clients are unaffected.
        iterations: bool = request.headers.get("x-hive-iterations", "").lower() == "true"

        # Extract the last user message as the primary input.
        user_parts = [m.get("content", "") for m in messages if m.get("role") == "user"]
        user_msg = user_parts[-1] if user_parts else ""
        if not user_msg:
            raise HTTPException(status_code=400, detail="no user message found in messages")

        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        if stream:
            if iterations:
                # Iteration-aware streaming: one tool-calling turn via the
                # orchestrator, emitting OpenAI-shaped chunks with
                # delta.tool_calls populated when the model decides to call.
                async def _stream_iterations():
                    import json
                    try:
                        async for ev in hive.stream_ask_iterations(
                            user_msg, session_id=session_id, channel_hint="api",
                        ):
                            ev_type = ev.get("type", "")
                            delta: dict = {}
                            if ev_type == "model_decision":
                                if ev.get("text"):
                                    delta["content"] = ev["text"]
                                tcs = ev.get("tool_calls") or []
                                if tcs:
                                    delta["tool_calls"] = [
                                        {"index": i, "id": tc["id"], "type": "function",
                                         "function": {"name": tc["name"],
                                                      "arguments": tc["arguments"]}}
                                        for i, tc in enumerate(tcs)
                                    ]
                            elif ev_type in ("tool_call_start", "tool_call_end",
                                             "loop_guard"):
                                # Markers so non-tool-aware OpenAI clients see
                                # the tool activity as readable content.
                                marker = {"type": ev_type, **ev}
                                delta["content"] = f"\n[{ev_type}] {json.dumps(marker)}\n"
                            chunk = {
                                "id": cid, "object": "chat.completion.chunk",
                                "created": created, "model": "hive",
                                "choices": [{"index": 0, "delta": delta,
                                             "finish_reason": None}],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                            if ev_type in ("final", "max_turns", "error"):
                                stop_chunk = {
                                    "id": cid, "object": "chat.completion.chunk",
                                    "created": created, "model": "hive",
                                    "choices": [{"index": 0, "delta": {},
                                                 "finish_reason": "stop"}],
                                }
                                yield f"data: {json.dumps(stop_chunk)}\n\n"
                    except Exception as exc:  # noqa: BLE001
                        log.error("v1 iterations stream error: %s", exc, exc_info=True)
                        # Surface the error to the client as a stop chunk with a
                        # sanitised class name (no message body / stack frames).
                        # Matches /chat/stream/iterations's `event: error` contract,
                        # in OpenAI-shape (delta.content + finish_reason: stop).
                        import json as _json_err
                        safe_name = type(exc).__name__.replace("\n", " ").replace("\r", " ")
                        err_chunk = {
                            "id": cid, "object": "chat.completion.chunk",
                            "created": created, "model": "hive",
                            "choices": [{"index": 0,
                                         "delta": {"content": f"\n[error] {safe_name}\n"},
                                         "finish_reason": "stop"}],
                        }
                        yield f"data: {_json_err.dumps(err_chunk)}\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(_stream_iterations(),
                                         media_type="text/event-stream",
                                         headers={"X-Accel-Buffering": "no"})

            async def _stream():
                try:
                    async for token in hive.ask_stream(user_msg, session_id=session_id,
                                                      channel_hint="api"):
                        chunk = {
                            "id": cid, "object": "chat.completion.chunk",
                            "created": created, "model": "hive",
                            "choices": [{"index": 0, "delta": {"content": token},
                                         "finish_reason": None}],
                        }
                        import json
                        yield f"data: {json.dumps(chunk)}\n\n"
                    stop_chunk = {
                        "id": cid, "object": "chat.completion.chunk",
                        "created": created, "model": "hive",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    import json
                    yield f"data: {json.dumps(stop_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception as exc:  # noqa: BLE001
                    log.error("v1/chat/completions stream error: %s", exc, exc_info=True)
                    # Surface the error to the client as a stop chunk with a
                    # sanitised class name (no message body / stack frames).
                    import json as _json_err
                    safe_name = type(exc).__name__.replace("\n", " ").replace("\r", " ")
                    err_chunk = {
                        "id": cid, "object": "chat.completion.chunk",
                        "created": created, "model": "hive",
                        "choices": [{"index": 0,
                                     "delta": {"content": f"\n[error] {safe_name}\n"},
                                     "finish_reason": "stop"}],
                    }
                    yield f"data: {_json_err.dumps(err_chunk)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(_stream(), media_type="text/event-stream",
                                     headers={"X-Accel-Buffering": "no"})

        reply = await hive.ask(user_msg, session_id=session_id, channel_hint="api")
        return {
            "id": cid, "object": "chat.completion", "created": created, "model": "hive",
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": reply},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    # Serve the Mission Control dashboard SPA if it has been built (opt-in).
    # Mount at /app so API routes take priority; `npm run build` in dashboard/ to enable.
    if _DASHBOARD_DIST.exists():
        from fastapi.staticfiles import StaticFiles
        app.mount("/app", StaticFiles(directory=str(_DASHBOARD_DIST), html=True),
                  name="dashboard")
        log.info("Mission Control dashboard served at /app")

    if telegram is not None:
        webhook_secret = hive.config.telegram_webhook_secret

        @app.post("/telegram/webhook")
        async def telegram_webhook(request: Request) -> dict:
            # Telegram authenticates webhooks via this header (set at setWebhook time).
            if not webhook_secret or request.headers.get(
                    "X-Telegram-Bot-Api-Secret-Token") != webhook_secret:
                raise HTTPException(status_code=401, detail="bad webhook secret")
            body = await request.body()
            if len(body) > MAX_WEBHOOK_BODY:
                raise HTTPException(status_code=413, detail="payload too large")
            try:
                update = await request.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram webhook: failed to parse request body: %s", exc)
                return {"ok": True, "handled": False}
            event = telegram.parse_update(update)
            if event is None:
                return {"ok": True, "handled": False}  # nothing actionable
            if not _sender_allowed(
                event,
                allowed_users=hive.config.telegram_allowed_user_ids,
                allowed_chats=hive.config.telegram_allowed_chat_ids,
            ):
                return _reject_unallowed_sender(event)
            try:
                reply = await hive.ask(event.text, session_id=f"telegram:{event.chat_id}",
                                      channel_hint="telegram")
                await telegram.send(OutgoingMessage(chat_id=event.chat_id, text=reply,
                                                    reply_to=event.message_id or None))
            except Exception as exc:  # noqa: BLE001
                log.error("telegram turn failed (chat=%s): %s", event.chat_id, exc,
                          exc_info=True)
                # Return 500 so Telegram retries transient failures (LLM outage, timeout).
                # Telegram backs off and eventually stops; permanent errors are logged above.
                raise HTTPException(status_code=500, detail="internal error") from exc
            return {"ok": True, "handled": True}

    # --- SPRINT_6 P-E: Slack / Discord / Email inbound (issue #73) -----

    if hive.config.slack_signing_secret:
        from hive.gateway.channels.slack import SlackChannel as _SlackChannel
        slack_channel: ChannelAdapter | None = _SlackChannel(
            bot_token=hive.config.slack_bot_token,
            signing_secret=hive.config.slack_signing_secret,
        )
    else:
        slack_channel = None

    if slack_channel is not None:
        @app.post("/slack/webhook")
        async def slack_webhook(request: Request) -> dict:
            body = await request.body()
            if len(body) > MAX_WEBHOOK_BODY:
                raise HTTPException(status_code=413, detail="payload too large")
            from hive.gateway.channels.slack import SlackChannel
            if not SlackChannel.verify_signature(request.headers, body,
                                                 hive.config.slack_signing_secret):
                raise HTTPException(status_code=401, detail="bad slack signature")
            try:
                payload = await request.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("slack webhook: failed to parse request body: %s", exc)
                return {"ok": True, "handled": False}
            if payload.get("type") == "url_verification":
                return {"challenge": payload.get("challenge")}
            event = slack_channel.parse_update(payload)
            if event is None:
                return {"ok": True, "handled": False}
            if not _sender_allowed(
                event, allowed_users=hive.config.slack_allowed_user_ids
            ):
                return _reject_unallowed_sender(event)
            try:
                reply = await hive.ask(event.text, session_id=f"slack:{event.chat_id}",
                                       channel_hint="slack")
                await slack_channel.send(OutgoingMessage(chat_id=event.chat_id,
                                                         text=reply))
            except Exception as exc:  # noqa: BLE001
                log.error("slack turn failed (chat=%s): %s", event.chat_id, exc,
                          exc_info=True)
                raise HTTPException(status_code=500, detail="internal error") from exc
            return {"ok": True, "handled": True}

    if hive.config.discord_public_key:
        from hive.gateway.channels.discord import DiscordChannel as _DiscordChannel
        discord_channel: ChannelAdapter | None = _DiscordChannel(
            bot_token=hive.config.discord_bot_token,
            public_key=hive.config.discord_public_key,
            application_id=hive.config.discord_application_id,
        )
    else:
        discord_channel = None

    if discord_channel is not None:
        @app.post("/discord/webhook")
        async def discord_webhook(request: Request) -> dict:
            body = await request.body()
            if len(body) > MAX_WEBHOOK_BODY:
                raise HTTPException(status_code=413, detail="payload too large")
            if not discord_channel.verify_signature(request.headers, body):
                raise HTTPException(status_code=401, detail="bad discord signature")
            try:
                payload = await request.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("discord webhook: failed to parse request body: %s", exc)
                return {"ok": True, "handled": False}
            if payload.get("t") == 0:
                return {"type": 1}
            event = discord_channel.parse_update(payload)
            if event is None:
                return {"ok": True, "handled": False}
            if not _sender_allowed(
                event, allowed_users=hive.config.discord_allowed_user_ids
            ):
                return _reject_unallowed_sender(event)
            try:
                reply = await hive.ask(event.text, session_id=f"discord:{event.chat_id}",
                                       channel_hint="discord")
                await discord_channel.send(OutgoingMessage(chat_id=event.chat_id,
                                                            text=reply))
            except Exception as exc:  # noqa: BLE001
                log.error("discord turn failed (chat=%s): %s", event.chat_id, exc,
                          exc_info=True)
                raise HTTPException(status_code=500, detail="internal error") from exc
            return {"ok": True, "handled": True}

    if hive.config.smtp_webhook_secret:
        from hive.gateway.channels.email import EmailChannel as _EmailChannel
        email_channel: ChannelAdapter | None = _EmailChannel(
            smtp_host=hive.config.smtp_host,
            smtp_port=hive.config.smtp_port,
            smtp_user=hive.config.smtp_user,
            smtp_pass=hive.config.smtp_pass,
            smtp_from=hive.config.smtp_from,
        )
    else:
        email_channel = None

    if email_channel is not None:
        @app.post("/email/webhook")
        async def email_webhook(request: Request) -> dict:
            secret = request.headers.get("X-Webhook-Secret", "")
            if not hmac.compare_digest(secret, hive.config.smtp_webhook_secret):
                raise HTTPException(status_code=401, detail="bad email webhook secret")
            raw = await request.body()
            if len(raw) > MAX_WEBHOOK_BODY:
                raise HTTPException(status_code=413, detail="payload too large")
            event = email_channel.parse_update({"raw_bytes": raw})
            if event is None:
                return {"ok": True, "handled": False}
            verified_sender = request.headers.get("X-Verified-Sender", "").strip()
            if (
                not event.raw.get("sender_verified", False)
                or not verified_sender
                or verified_sender.casefold() != event.user_id.casefold()
            ):
                return _reject_unallowed_sender(event, reason="sender_not_verified")
            if not _sender_allowed(
                event,
                allowed_users=hive.config.email_allowed_senders,
                casefold=True,
            ):
                return _reject_unallowed_sender(event)
            try:
                # session_id uses message_id (unspoofable, globally unique), not
                # chat_id — a crafted From header cannot reuse a past session.
                sid = f"email:{event.message_id}" if event.message_id else f"email:{event.chat_id}"
                reply = await hive.ask(event.text, session_id=sid, channel_hint="email")
                await email_channel.send(OutgoingMessage(chat_id=event.chat_id,
                                                          text=reply,
                                                          reply_to=event.message_id or None))
            except Exception as exc:  # noqa: BLE001
                log.error("email turn failed (chat=%s): %s", event.chat_id, exc,
                          exc_info=True)
                raise HTTPException(status_code=500, detail="internal error") from exc
            return {"ok": True, "handled": True}

    # --- A2A envelope endpoint (SPRINT_6 P-D, issue #72) -----------------

    @app.post("/a2a/rpc", dependencies=[Depends(require_token)])
    async def a2a_rpc(body: dict) -> dict:
        """Route an A2A envelope to a local registered handler.

        Body shape: ``{"id": str, "method": str, "params": dict}``.
        Returns the A2A response envelope as a JSON dict. Only locally
        registered methods are dispatched; remote URIs return a routing
        hint so the caller can dispatch via A2AClient."""
        from hive.agents.a2a.envelope import A2ARequest, A2AResponse
        from hive.agents.a2a.router import route as _a2a_route
        try:
            req = A2ARequest.model_validate(body)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f"invalid envelope: {exc}") from exc
        resp: A2AResponse = await _a2a_route(req.id, req.method, req.params)
        return resp.model_dump(exclude_none=True)

    # --- Learning loop endpoints (SPRINT_6 P-F) --------------------------

    @app.get("/learning/status", dependencies=[Depends(require_token)])
    async def learning_status() -> dict:
        """Counts + most-recent loop outcomes."""
        from hive.core.learning import storage
        db_path = str(hive.config.state_db)
        counts = storage.count_by_verdict(db_path)
        recent = storage.query_loops(db_path, limit=10)
        return {
            "enabled": bool(hive.config.learning_loop_enabled),
            "counts": counts,
            "recent": [
                {
                    "id": o.id, "ts": o.ts, "symptom": o.symptom,
                    "verdict": o.verdict,
                    "pytest_baseline": o.pytest_baseline,
                    "pytest_candidate": o.pytest_candidate,
                    "evals_baseline": o.evals_baseline,
                    "evals_candidate": o.evals_candidate,
                    "worktree_branch": o.worktree_branch,
                    "pr_url": o.pr_url,
                    "reject_reason": o.reject_reason,
                }
                for o in recent
            ],
        }

    @app.get("/learning/history", dependencies=[Depends(require_token)])
    async def learning_history(limit: int = 50) -> dict:
        """Last N learning-loop outcomes (newest first)."""
        from hive.core.learning import storage
        limit = max(1, min(200, int(limit)))  # cap at 200 to avoid runaway
        loops = storage.query_loops(str(hive.config.state_db), limit=limit)
        return {
            "count": len(loops),
            "loops": [
                {
                    "id": o.id, "ts": o.ts, "symptom": o.symptom,
                    "verdict": o.verdict,
                    "pytest_baseline": o.pytest_baseline,
                    "pytest_candidate": o.pytest_candidate,
                    "evals_baseline": o.evals_baseline,
                    "evals_candidate": o.evals_candidate,
                    "worktree_branch": o.worktree_branch,
                    "pr_url": o.pr_url,
                    "reject_reason": o.reject_reason,
                }
                for o in loops
            ],
        }

    @app.post("/learning/run", dependencies=[Depends(require_token)])
    async def learning_run(body: dict) -> dict:
        """Trigger one learning-loop iteration for the given symptom.

        Body: ``{"symptom": "..."}``. The loop runs synchronously here
        (operator-curated entry point, not the heartbeat path) and returns
        the resulting LoopOutcome dict.
        """
        symptom = str(body.get("symptom", "")).strip()
        if not symptom:
            raise HTTPException(
                status_code=400, detail="symptom is required",
            )
        outcome = await hive.self_improve_from_symptom(
            symptom, use_learning_loop=True,
        )
        return {"outcome_count": len(outcome), "symptom": symptom}

    return app
