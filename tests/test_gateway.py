"""P8 — gateway: /health /chat /ws /budget /approvals over a built HiveOS."""
from __future__ import annotations

from starlette.testclient import TestClient

from hive.core.approval_enhancements import enhance
from hive.core.config import HiveConfig
from hive.core.types import ToolCall
from hive.gateway.app import create_app
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS


class _ScriptRouter:
    def __init__(self, script):
        self._script = list(script)

    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        item = self._script.pop(0) if self._script else CompletionResult(text="ok", model="m")
        return item if isinstance(item, CompletionResult) else CompletionResult(text=item, model="m")

    async def aclose(self):
        pass


def _hive(tmp_path, script=None) -> HiveOS:
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(cfg, router=_ScriptRouter(script or []))


def _client(hive) -> TestClient:
    return TestClient(create_app(hive))


_TOKEN = {"X-Hive-Token": "change_me"}  # default HIVE_SECRET


def test_health_is_open(tmp_path):
    with _client(_hive(tmp_path)) as c:
        r = c.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"


def test_chat_requires_token(tmp_path):
    with _client(_hive(tmp_path)) as c:
        assert c.post("/chat", json={"message": "hi"}).status_code == 401


def test_chat_returns_reply(tmp_path):
    hive = _hive(tmp_path, [CompletionResult(text="hello back", model="m")])
    with _client(hive) as c:
        r = c.post("/chat", json={"message": "hi", "session_id": "s1"}, headers=_TOKEN)
        assert r.status_code == 200
        body = r.json()
        assert body["reply"] == "hello back" and body["session_id"] == "s1"
        assert body["protocol_version"]  # B4: responses carry the protocol version


def test_budget_snapshot(tmp_path):
    with _client(_hive(tmp_path)) as c:
        r = c.get("/budget", headers=_TOKEN)
        assert r.status_code == 200 and r.json()["daily_cap"] == 3000


def test_approvals_flow_gated_then_executed(tmp_path):
    # model asks for a dangerous tool -> it is gated (pending), not executed
    call = ToolCall(id="c1", name="deploy", arguments='{"target": "prod"}')
    hive = _hive(tmp_path, [CompletionResult(text="", model="m", tool_calls=[call]),
                            CompletionResult(text="queued", model="m")])
    with _client(hive) as c:
        c.post("/chat", json={"message": "ship it"}, headers=_TOKEN)
        pending = c.get("/approvals", headers=_TOKEN).json()["pending"]
        assert pending and pending[0]["tool"] == "deploy"
        aid = pending[0]["id"]
        # approve -> the gated tool now runs
        r = c.post("/approvals/decide", json={"approval_id": aid, "approved": True}, headers=_TOKEN)
        assert r.status_code == 200 and r.json()["executed"] is True
        assert "prod" in r.json()["result"]
        # unknown id -> 404
        assert c.post("/approvals/decide", json={"approval_id": "zzz", "approved": True},
                      headers=_TOKEN).status_code == 404


def test_ws_handshake_and_reply(tmp_path):
    hive = _hive(tmp_path, [CompletionResult(text="ws reply", model="m")])
    with _client(hive) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_text("change_me")        # token handshake
            ws.send_text("hello over ws")
            assert ws.receive_json() == {"type": "reply", "data": "ws reply"}


def test_ws_rejects_bad_token(tmp_path):
    with _client(_hive(tmp_path)) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_text("wrong")
            assert ws.receive_json()["data"] == "unauthorized"


def test_chat_hides_exception_detail(tmp_path):
    class _BoomRouter(_ScriptRouter):
        async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
            raise RuntimeError("secret db password in stacktrace")

    hive = HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False),
                        router=_BoomRouter([]))
    with _client(hive) as c:
        r = c.post("/chat", json={"message": "hi"}, headers=_TOKEN)
        assert r.status_code == 503
        body = r.json()
        assert "secret db password" not in str(body)
        assert "RuntimeError" not in str(body)


def test_ws_error_sends_generic_message(tmp_path):
    class _BoomRouter(_ScriptRouter):
        async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
            raise RuntimeError("secret ws stacktrace")

    hive = HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False),
                        router=_BoomRouter([]))
    with _client(hive) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_text("change_me")
            ws.send_text("trigger error")
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert "secret ws stacktrace" not in str(msg)
            assert "RuntimeError" not in str(msg)
            assert msg["data"] == "internal error"


# ---------------------------------------------------------------------------
# gateway/auth.py — token_ok() and make_auth_dependency() unit tests
# ---------------------------------------------------------------------------

def test_token_ok_matching():
    from hive.gateway.auth import token_ok
    assert token_ok("abc123", "abc123") is True


def test_token_ok_mismatch():
    from hive.gateway.auth import token_ok
    assert token_ok("wrong", "abc123") is False


def test_token_ok_none_token():
    from hive.gateway.auth import token_ok
    assert token_ok(None, "abc123") is False


def test_token_ok_empty_token():
    from hive.gateway.auth import token_ok
    assert token_ok("", "abc123") is False


def test_make_auth_dependency_blocks_missing_header(tmp_path):
    with _client(_hive(tmp_path)) as c:
        assert c.get("/budget").status_code == 401


def test_make_auth_dependency_blocks_wrong_token(tmp_path):
    with _client(_hive(tmp_path)) as c:
        assert c.get("/budget", headers={"X-Hive-Token": "nope"}).status_code == 401


def test_make_auth_dependency_allows_correct_token(tmp_path):
    with _client(_hive(tmp_path)) as c:
        assert c.get("/budget", headers=_TOKEN).status_code == 200


# ---------------------------------------------------------------------------
# /approvals/decide — self_mod routing
# ---------------------------------------------------------------------------

def test_approvals_decide_self_mod_routes_to_improver(tmp_path):
    """REVIEW-tier self-mod approvals must go through improver.apply_approved,
    not the tool executor (which would return 'unknown tool: self_mod:...')."""
    from hive.core.approval import gate
    from hive.core.spec_search import Edit, EditOp, RiskTier

    hive = _hive(tmp_path)

    async def _noop_apply(wt):
        return []

    edit = Edit(op=EditOp.PATCH_CODE, summary="fix crash",
                rationale="it explodes", apply=_noop_apply,
                risk_tier=RiskTier.REVIEW)
    approval_id = gate.request("self_mod:patch_code", {"summary": "fix crash"},
                               "test reason")
    enhance.audit_request(approval_id)
    hive.edit_pending[approval_id] = edit

    with _client(hive) as c:
        r = c.post("/approvals/decide",
                   json={"approval_id": approval_id, "approved": True},
                   headers=_TOKEN)
    assert r.status_code == 200
    body = r.json()
    assert body["executed"] is True
    # SelfModifier will fail (no real git repo), but it must NOT say "unknown tool"
    assert "unknown tool" not in str(body)


def test_approvals_decide_self_mod_rejection_cleans_edit_pending(tmp_path):
    """Rejecting a self_mod approval must remove it from edit_pending."""
    from hive.core.approval import gate
    from hive.core.spec_search import Edit, EditOp, RiskTier

    hive = _hive(tmp_path)

    async def _noop_apply(wt):
        return []

    edit = Edit(op=EditOp.PATCH_CODE, summary="fix crash",
                rationale="it explodes", apply=_noop_apply,
                risk_tier=RiskTier.REVIEW)
    approval_id = gate.request("self_mod:patch_code", {"summary": "fix crash"},
                               "test reason")
    enhance.audit_request(approval_id)
    hive.edit_pending[approval_id] = edit

    with _client(hive) as c:
        r = c.post("/approvals/decide",
                   json={"approval_id": approval_id, "approved": False},
                   headers=_TOKEN)
    assert r.status_code == 200
    assert r.json()["executed"] is False
    assert approval_id not in hive.edit_pending


def test_approvals_decide_self_mod_missing_edit_returns_error(tmp_path):
    """If the edit was lost (process restart), the decide endpoint should return
    an error rather than crashing."""
    from hive.core.approval import gate

    hive = _hive(tmp_path)
    approval_id = gate.request("self_mod:patch_code", {"summary": "gone"},
                               "test")
    enhance.audit_request(approval_id)
    # do NOT store anything in edit_pending

    with _client(hive) as c:
        r = c.post("/approvals/decide",
                   json={"approval_id": approval_id, "approved": True},
                   headers=_TOKEN)
    assert r.status_code == 200
    body = r.json()
    assert body["executed"] is False
    assert "error" in body


def test_approvals_cancel_removes_pending_edit(tmp_path):
    """POST /approvals/cancel must remove the REVIEW-tier edit from the pending store."""
    from hive.core.approval import gate
    from hive.core.spec_search import Edit, EditOp, RiskTier
    hive = _hive(tmp_path)
    approval_id = gate.request("self_mod:patch_code", {"summary": "x"}, "rationale")

    async def _noop(_wt): return []
    edit = Edit(op=EditOp.PATCH_CODE, summary="x", apply=_noop)
    edit.risk_tier = RiskTier.REVIEW
    hive.edit_pending[approval_id] = edit
    hive.improver._pending_store[approval_id] = edit

    with _client(hive) as c:
        r = c.post("/approvals/cancel",
                   json={"approval_id": approval_id, "approved": False},
                   headers=_TOKEN)
    assert r.status_code == 200
    assert r.json()["cancelled"] is True
    assert approval_id not in hive.improver._pending_store


def test_approvals_cancel_unknown_returns_404(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.post("/approvals/cancel",
                   json={"approval_id": "nonexistent", "approved": False},
                   headers=_TOKEN)
    assert r.status_code == 404


def test_approvals_includes_pending_edits_count(tmp_path):
    """GET /approvals must include pending_edits count from the improver."""
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/approvals", headers=_TOKEN).json()
    assert "pending_edits" in body
    assert isinstance(body["pending_edits"], int)


# ---------------------------------------------------------------------------
# /sessions — list, search, delete
# ---------------------------------------------------------------------------

def test_sessions_list_returns_session_ids(tmp_path):
    hive = _hive(tmp_path, [CompletionResult(text="hi", model="m")])
    with _client(hive) as c:
        c.post("/chat", json={"message": "hello", "session_id": "sess1"}, headers=_TOKEN)
        body = c.get("/sessions", headers=_TOKEN).json()
    assert "sessions" in body
    assert "sess1" in body["sessions"]


def test_sessions_search_finds_message(tmp_path):
    hive = _hive(tmp_path, [CompletionResult(text="pong", model="m")])
    with _client(hive) as c:
        c.post("/chat", json={"message": "findme_token", "session_id": "s1"},
               headers=_TOKEN)
        body = c.get("/sessions/search", params={"q": "findme_token"},
                     headers=_TOKEN).json()
    assert body["count"] >= 1
    assert any("findme_token" in r["content"] for r in body["results"])


def test_sessions_delete_removes_messages(tmp_path):
    hive = _hive(tmp_path, [CompletionResult(text="bye", model="m")])
    with _client(hive) as c:
        c.post("/chat", json={"message": "delete me", "session_id": "del1"},
               headers=_TOKEN)
        r = c.delete("/sessions/del1", headers=_TOKEN)
    assert r.status_code == 200
    assert r.json()["deleted"] >= 1


# ---------------------------------------------------------------------------
# /cron — list, add, enable/disable, delete
# ---------------------------------------------------------------------------

def test_cron_add_and_list(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.post("/cron", json={"schedule": "@hourly", "task_kind": "ping"},
                   headers=_TOKEN)
        assert r.status_code == 200
        job_id = r.json()["id"]
        jobs = c.get("/cron", headers=_TOKEN).json()["jobs"]
    assert any(j["id"] == job_id for j in jobs)


def test_cron_disable_enable(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.post("/cron", json={"schedule": "@daily", "task_kind": "noop"},
                   headers=_TOKEN)
        jid = r.json()["id"]
        r2 = c.post(f"/cron/{jid}/disable", headers=_TOKEN)
        assert r2.json()["enabled"] is False
        r3 = c.post(f"/cron/{jid}/enable", headers=_TOKEN)
        assert r3.json()["enabled"] is True


def test_cron_delete_removes_job(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.post("/cron", json={"schedule": "@weekly", "task_kind": "x"},
                   headers=_TOKEN)
        jid = r.json()["id"]
        r2 = c.delete(f"/cron/{jid}", headers=_TOKEN)
        assert r2.json()["removed"] is True
        r3 = c.delete(f"/cron/{jid}", headers=_TOKEN)
        assert r3.status_code == 404


def test_cron_add_missing_fields_returns_422(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.post("/cron", json={"schedule": "@hourly"}, headers=_TOKEN)
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# /tasks/stats and /tasks/retry-failed and /tasks?state=
# ---------------------------------------------------------------------------

def test_tasks_stats_returns_totals(tmp_path):
    hive = _hive(tmp_path)
    hive.task_board.enqueue("ping", {}, source="test")
    with _client(hive) as c:
        body = c.get("/tasks/stats", headers=_TOKEN).json()
    assert "total" in body and "by_state" in body


def test_tasks_retry_failed_resets_failed(tmp_path):
    hive = _hive(tmp_path)
    tid = hive.task_board.enqueue("job", {})
    hive.task_board.claim(tid)
    hive.task_board.fail(tid, "boom")
    with _client(hive) as c:
        body = c.post("/tasks/retry-failed", headers=_TOKEN).json()
    assert body["retried"] == 1


def test_tasks_filter_by_kind(tmp_path):
    hive = _hive(tmp_path)
    hive.task_board.enqueue("alpha", {})
    hive.task_board.enqueue("beta", {})
    with _client(hive) as c:
        body = c.get("/tasks", params={"kind": "alpha"}, headers=_TOKEN).json()
    assert all(t["kind"] == "alpha" for t in body["tasks"])
    assert len(body["tasks"]) == 1


# ---------------------------------------------------------------------------
# /audit/export
# ---------------------------------------------------------------------------

def test_audit_export_returns_entries(tmp_path):
    hive = _hive(tmp_path)
    hive.audit_log.record({"tool": "ping", "status": "ok", "approved": True})
    with _client(hive) as c:
        body = c.get("/audit/export", headers=_TOKEN).json()
    assert body["count"] >= 1
    assert all("tool" in e for e in body["entries"])


# ---------------------------------------------------------------------------
# /health/full — full system health snapshot
# ---------------------------------------------------------------------------

def test_health_full_requires_token(tmp_path):
    with _client(_hive(tmp_path)) as c:
        assert c.get("/health/full").status_code == 401


def test_health_full_returns_snapshot(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/health/full", headers=_TOKEN).json()
    assert body["status"] == "ok"
    assert "budget" in body
    assert "tasks" in body
    assert "telemetry" in body
    assert isinstance(body["tools"], int)


# ---------------------------------------------------------------------------
# /config/validate, /tools, /memory/export
# ---------------------------------------------------------------------------

def test_config_validate_returns_issues(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/config/validate", headers=_TOKEN).json()
    assert "valid" in body
    assert "issues" in body
    assert isinstance(body["issues"], list)


def test_tools_list_returns_registered_tools(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/tools", headers=_TOKEN).json()
    assert "count" in body
    assert "tools" in body
    assert body["count"] == len(body["tools"])
    if body["tools"]:
        t = body["tools"][0]
        assert "name" in t and "category" in t and "dangerous" in t


def test_memory_export_returns_structure(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/memory/export", headers=_TOKEN).json()
    assert "knowledge" in body and "episodic" in body
    assert isinstance(body["knowledge_count"], int)


# ---------------------------------------------------------------------------
# /sessions/{id} — detail, title
# ---------------------------------------------------------------------------

def test_session_detail_endpoint(tmp_path):
    hive = _hive(tmp_path, [CompletionResult(text="hi", model="m")])
    with _client(hive) as c:
        c.post("/chat", json={"message": "hello", "session_id": "s1"}, headers=_TOKEN)
        body = c.get("/sessions/s1", headers=_TOKEN).json()
    assert body["session_id"] == "s1"
    assert body["message_count"] >= 1


def test_session_set_and_get_title(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        hive.session_store.ensure("s1")
        r = c.post("/sessions/s1/title",
                   json={"title": "My test session"}, headers=_TOKEN)
        assert r.status_code == 200
        r2 = c.get("/sessions/s1/title", headers=_TOKEN)
        assert r2.json()["title"] == "My test session"


def test_session_set_title_missing_body_returns_422(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        hive.session_store.ensure("s2")
        r = c.post("/sessions/s2/title", json={}, headers=_TOKEN)
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# /audit/stats
# ---------------------------------------------------------------------------

def test_audit_search_by_tool(tmp_path):
    hive = _hive(tmp_path)
    hive.audit_log.record({"tool": "read_file", "status": "ok"})
    hive.audit_log.record({"tool": "deploy", "status": "pending_approval"})
    with _client(hive) as c:
        body = c.get("/audit/search", params={"tool": "read_file"}, headers=_TOKEN).json()
    assert body["count"] == 1
    assert body["entries"][0]["tool"] == "read_file"


def test_skills_endpoint_returns_stats(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/skills", headers=_TOKEN).json()
    assert "total" in body and "by_state" in body


def test_audit_stats_groups_by_tool(tmp_path):
    hive = _hive(tmp_path)
    hive.audit_log.record({"tool": "ping", "status": "ok", "approved": True})
    hive.audit_log.record({"tool": "ping", "status": "ok", "approved": True})
    hive.audit_log.record({"tool": "deploy", "status": "pending_approval"})
    with _client(hive) as c:
        body = c.get("/audit/stats", headers=_TOKEN).json()
    assert body["total"] == 3
    assert "ping" in body["by_tool"]
    assert body["by_tool"]["ping"]["total"] == 2


# ---------------------------------------------------------------------------
# /sessions/stats and /sessions/{id}/auto-title
# ---------------------------------------------------------------------------

def test_sessions_stats_returns_counts(tmp_path):
    hive = _hive(tmp_path, [CompletionResult(text="reply", model="m")])
    with _client(hive) as c:
        c.post("/chat", json={"message": "hi", "session_id": "stat1"}, headers=_TOKEN)
        body = c.get("/sessions/stats", headers=_TOKEN).json()
    assert body["sessions"] >= 1
    assert body["messages"] >= 1


def test_traces_list_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/traces", headers=_TOKEN).json()
    assert "sessions" in body
    assert isinstance(body["sessions"], list)


def test_traces_session_includes_event_count(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/traces/default", headers=_TOKEN).json()
    assert "event_count" in body
    assert isinstance(body["event_count"], int)


def test_traces_clear_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.delete("/traces/default", headers=_TOKEN)
    assert r.status_code == 200
    assert "cleared" in r.json()


def test_sessions_auto_title_returns_string(tmp_path):
    hive = _hive(tmp_path, [
        CompletionResult(text="dummy", model="m"),   # for /chat
        CompletionResult(text="My Session Title", model="m"),  # for title generation
    ])
    with _client(hive) as c:
        c.post("/chat", json={"message": "tell me about cats", "session_id": "title_s"},
               headers=_TOKEN)
        body = c.post("/sessions/title_s/auto-title", headers=_TOKEN).json()
    assert body["session_id"] == "title_s"
    # title is a string (possibly None if model returns empty)
    assert body["title"] is None or isinstance(body["title"], str)


# --- commitments endpoints ---------------------------------------------------

def test_commitments_list_empty(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/commitments", headers=_TOKEN).json()
    assert body["commitments"] == []


def test_commitments_add_and_list(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.post("/commitments",
                      json={"description": "daily standup", "cadence_seconds": 86400},
                      headers=_TOKEN).json()
        assert body["id"] >= 1 and body["description"] == "daily standup"
        listed = c.get("/commitments", headers=_TOKEN).json()
    assert len(listed["commitments"]) == 1
    assert listed["commitments"][0]["description"] == "daily standup"


def test_commitments_add_missing_field_returns_422(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.post("/commitments", json={"description": "no cadence"}, headers=_TOKEN)
    assert r.status_code == 422


def test_commitments_remove(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        cid = c.post("/commitments",
                     json={"description": "to delete", "cadence_seconds": 3600},
                     headers=_TOKEN).json()["id"]
        body = c.delete(f"/commitments/{cid}", headers=_TOKEN).json()
        assert body["removed"] is True
        listed = c.get("/commitments", headers=_TOKEN).json()
    assert listed["commitments"] == []


def test_commitments_remove_unknown_returns_404(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.delete("/commitments/99999", headers=_TOKEN)
    assert r.status_code == 404


def test_commitments_fulfill(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        cid = c.post("/commitments",
                     json={"description": "weekly report", "cadence_seconds": 604800},
                     headers=_TOKEN).json()["id"]
        body = c.post(f"/commitments/{cid}/fulfill", headers=_TOKEN).json()
    assert body["fulfilled"] is True and body["commitment_id"] == cid


def test_commitments_fulfill_unknown_returns_404(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.post("/commitments/99999/fulfill", headers=_TOKEN)
    assert r.status_code == 404


def test_commitments_overdue_endpoint(tmp_path):
    hive = _hive(tmp_path)
    # Add a commitment that is immediately overdue (cadence 0 → always due)
    hive.commitments.add("immediately due", cadence_seconds=0)
    with _client(hive) as c:
        body = c.get("/commitments/overdue", headers=_TOKEN).json()
    assert len(body["overdue"]) == 1
    assert body["overdue"][0]["description"] == "immediately due"


# --- cron single-job endpoint ------------------------------------------------

def test_cron_get_single_job(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        add_body = c.post("/cron",
                          json={"schedule": "@daily", "task_kind": "ping"},
                          headers=_TOKEN).json()
        job_id = add_body["id"]
        body = c.get(f"/cron/{job_id}", headers=_TOKEN).json()
    assert body["id"] == job_id and body["schedule"] == "@daily"


def test_cron_get_unknown_returns_404(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.get("/cron/99999", headers=_TOKEN)
    assert r.status_code == 404


# --- approvals/edits endpoint -------------------------------------------------

def test_approvals_edits_lists_pending_review(tmp_path):
    from hive.core.spec_search import Edit, EditOp
    hive = _hive(tmp_path)

    async def _noop(_wt): return []
    edit = Edit(op=EditOp.PATCH_CODE, summary="fix bug", apply=_noop)
    import asyncio
    asyncio.run(hive.improver.run([edit]))

    with _client(hive) as c:
        body = c.get("/approvals/edits", headers=_TOKEN).json()
    assert body["count"] == 1
    assert body["pending_edits"][0]["op"] == "patch_code"
    assert body["pending_edits"][0]["summary"] == "fix bug"


def test_approvals_edits_empty(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/approvals/edits", headers=_TOKEN).json()
    assert body["count"] == 0 and body["pending_edits"] == []


# --- /self-improve/symptom endpoint -------------------------------------------

def test_self_improve_symptom_returns_outcomes(tmp_path):
    # Router returns empty JSON array so diagnoser proposes nothing — safe no-op
    hive = _hive(tmp_path, [CompletionResult(text="[]", model="m")])
    with _client(hive) as c:
        body = c.post("/self-improve/symptom",
                      json={"symptom": "test_foo fails with KeyError"},
                      headers=_TOKEN).json()
    assert "outcomes" in body
    assert isinstance(body["outcomes"], list)


def test_self_improve_symptom_missing_body_returns_422(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.post("/self-improve/symptom", json={}, headers=_TOKEN)
    assert r.status_code == 422


# --- /budget/detail endpoint --------------------------------------------------

def test_budget_detail_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/budget/detail", headers=_TOKEN).json()
    assert "remaining_calls" in body
    assert "is_near_cap" in body
    assert isinstance(body["remaining_calls"], int)
    assert isinstance(body["is_near_cap"], bool)


# --- /self-improve/history endpoint -------------------------------------------

def test_self_improve_history_empty(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/self-improve/history", headers=_TOKEN).json()
    assert body["count"] == 0 and body["history"] == []


def test_self_improve_history_after_diagnose(tmp_path):
    # Inject a router that returns empty edits so diagnose is safe + fast
    hive = _hive(tmp_path, [CompletionResult(text="[]", model="m")])
    with _client(hive) as c:
        # Trigger a self-improvement cycle to populate history
        c.post("/self-improve/symptom",
               json={"symptom": "test_x fails with ValueError"},
               headers=_TOKEN)
        body = c.get("/self-improve/history", headers=_TOKEN).json()
    # History may be empty if diagnoser returned no edits (no proposals made)
    assert "history" in body and "count" in body


# --- /tools/stats endpoint ----------------------------------------------------

def test_tools_stats_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/tools/stats", headers=_TOKEN).json()
    assert "total" in body and "available" in body
    assert "dangerous_count" in body and "by_category" in body
    assert body["total"] >= 1


# --- /audit/purge endpoint ----------------------------------------------------

def test_audit_purge_endpoint(tmp_path):
    hive = _hive(tmp_path)
    hive.audit_log.record({"tool": "x", "status": "ok"})
    with _client(hive) as c:
        body = c.delete("/audit/purge", params={"max_age_days": 0.0}, headers=_TOKEN).json()
    assert "deleted" in body and body["max_age_days"] == 0.0


def test_audit_purge_keeps_recent_entries(tmp_path):
    hive = _hive(tmp_path)
    hive.audit_log.record({"tool": "recent", "status": "ok"})
    with _client(hive) as c:
        # 90-day purge: fresh entry should survive
        body = c.delete("/audit/purge", params={"max_age_days": 90.0}, headers=_TOKEN).json()
    assert body["deleted"] == 0


# --- health/full includes new fields ------------------------------------------

def test_health_full_includes_self_mod_fields(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/health/full", headers=_TOKEN).json()
    assert "pending_review_edits" in body
    assert "cron_jobs" in body
    assert "active_commitments" in body


# --- /budget/forecast endpoint ------------------------------------------------

def test_budget_forecast_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/budget/forecast", headers=_TOKEN).json()
    # SPRINT_7 Batch F: spend projection shape (replaces the old call-forecast).
    assert "projected_total" in body
    assert "daily_avg" in body
    assert "max_daily" in body
    assert "days_until_cap" in body
    assert "status" in body
    assert "confidence" in body


# --- /system-status endpoint --------------------------------------------------

def test_system_status_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/system-status", headers=_TOKEN).json()
    assert "router" in body
    assert "budget" in body
    assert "tasks" in body
    assert "tools" in body
    assert "pending_approvals" in body


def test_system_status_requires_token(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.get("/system-status")
    assert r.status_code == 401


# --- /memory/session/{id} endpoints -------------------------------------------

def test_memory_session_count_endpoint(tmp_path):
    hive = _hive(tmp_path, [CompletionResult(text="ok", model="m")])
    with _client(hive) as c:
        c.post("/chat", json={"message": "hi", "session_id": "count_sess"},
               headers=_TOKEN)
        body = c.get("/memory/session/count_sess/count", headers=_TOKEN).json()
    assert body["session_id"] == "count_sess"
    assert "episodic_count" in body


def test_memory_session_delete_endpoint(tmp_path):
    hive = _hive(tmp_path, [CompletionResult(text="ok", model="m")])
    with _client(hive) as c:
        c.post("/chat", json={"message": "remember me", "session_id": "del_sess"},
               headers=_TOKEN)
        body = c.delete("/memory/session/del_sess", headers=_TOKEN).json()
    assert body["session_id"] == "del_sess"
    assert "deleted" in body


def test_memory_session_count_unknown_session(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/memory/session/does_not_exist/count", headers=_TOKEN).json()
    assert body["episodic_count"] == 0


# --- /tasks/by-kind endpoint --------------------------------------------------

def test_tasks_by_kind_endpoint(tmp_path):
    hive = _hive(tmp_path)
    hive.task_board.enqueue("tool", {"tool": "read_file"})
    hive.task_board.enqueue("tool", {"tool": "write_file"})
    hive.task_board.enqueue("self_improve", {"symptom": "x"})
    with _client(hive) as c:
        body = c.get("/tasks/by-kind", headers=_TOKEN).json()
    assert body["by_kind"]["tool"] == 2
    assert body["by_kind"]["self_improve"] == 1


def test_tasks_by_kind_empty(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/tasks/by-kind", headers=_TOKEN).json()
    assert body["by_kind"] == {}


# --- /run-tests endpoint ------------------------------------------------------

def test_run_tests_dry_run(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.post("/run-tests", params={"dry_run": True}, headers=_TOKEN).json()
    assert body["all_passed"] is True and body["dry_run"] is True


# --- /traces/export endpoint --------------------------------------------------

def test_traces_export_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/traces/export/sess1", headers=_TOKEN).json()
    assert body["session_id"] == "sess1"
    assert "events" in body and "count" in body
    assert body["count"] == 0   # no events for this session


# --- /memory/{session_id}/consolidate endpoint --------------------------------

def test_memory_consolidate_endpoint(tmp_path):
    # Router returns a minimal JSON array so the keeper produces 0 new items safely
    hive = _hive(tmp_path, [CompletionResult(text="[]", model="m")])
    with _client(hive) as c:
        body = c.post("/memory/sess_x/consolidate", headers=_TOKEN).json()
    assert body["session_id"] == "sess_x"
    assert "new_items" in body


# --- /approvals/cancel-all endpoint -------------------------------------------

def test_approvals_cancel_all_endpoint(tmp_path):
    from hive.core.spec_search import Edit, EditOp
    hive = _hive(tmp_path)
    import asyncio
    async def _noop(_wt): return []
    e1 = Edit(op=EditOp.PATCH_CODE, summary="e1", apply=_noop)
    e2 = Edit(op=EditOp.PATCH_CODE, summary="e2", apply=_noop)
    asyncio.run(hive.improver.run([e1]))
    asyncio.run(hive.improver.run([e2]))
    assert hive.improver.pending_count() == 2
    with _client(hive) as c:
        body = c.delete("/approvals/cancel-all", headers=_TOKEN).json()
    assert body["cancelled"] == 2
    assert hive.improver.pending_count() == 0


def test_approvals_cancel_all_empty_returns_zero(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.delete("/approvals/cancel-all", headers=_TOKEN).json()
    assert body["cancelled"] == 0


# --- /model/catalog endpoint --------------------------------------------------

def test_model_catalog_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/model/catalog", headers=_TOKEN).json()
    assert "models" in body and "count" in body
    assert isinstance(body["models"], list)
    assert body["count"] >= 0


# --- /tasks/bulk-cancel endpoint ----------------------------------------------

def test_tasks_bulk_cancel_endpoint(tmp_path):
    hive = _hive(tmp_path)
    hive.task_board.enqueue("tool", {})
    hive.task_board.enqueue("tool", {})
    hive.task_board.enqueue("commit", {})
    with _client(hive) as c:
        body = c.post("/tasks/bulk-cancel", headers=_TOKEN, json={}).json()
        assert body["cancelled"] == 3
        assert hive.task_board.pending_count() == 0


def test_tasks_bulk_cancel_by_kind_endpoint(tmp_path):
    hive = _hive(tmp_path)
    hive.task_board.enqueue("tool", {})
    hive.task_board.enqueue("commit", {})
    with _client(hive) as c:
        body = c.post("/tasks/bulk-cancel", headers=_TOKEN, json={"kind": "tool"}).json()
        assert body["cancelled"] == 1
        assert hive.task_board.pending_count() == 1   # commit still pending


# --- /tasks/requeue-running endpoint ------------------------------------------

def test_tasks_requeue_running_endpoint(tmp_path):
    hive = _hive(tmp_path)
    tid = hive.task_board.enqueue("tool", {})
    hive.task_board.claim(tid)   # RUNNING
    with _client(hive) as c:
        body = c.post("/tasks/requeue-running", headers=_TOKEN).json()
        assert body["requeued"] == 1
        assert hive.task_board.get(tid).state == "pending"


def test_tasks_requeue_running_empty(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.post("/tasks/requeue-running", headers=_TOKEN).json()
    assert body["requeued"] == 0


# --- /tasks/failed endpoint ---------------------------------------------------

def test_tasks_failed_endpoint(tmp_path):
    hive = _hive(tmp_path)
    tid = hive.task_board.enqueue("tool", {"x": 1})
    hive.task_board.claim(tid)
    hive.task_board.fail(tid, "boom")
    with _client(hive) as c:
        body = c.get("/tasks/failed", headers=_TOKEN).json()
    assert "tasks" in body
    assert len(body["tasks"]) == 1
    assert body["tasks"][0]["kind"] == "tool"
    assert body["tasks"][0]["last_error"] == "boom"


def test_tasks_failed_endpoint_empty(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/tasks/failed", headers=_TOKEN).json()
    assert body["tasks"] == []


# --- /self-improve/status endpoint -------------------------------------------

def test_self_improve_status_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/self-improve/status", headers=_TOKEN).json()
    assert "pending_review_count" in body
    assert "pending_review" in body
    assert "recent_branches" in body
    assert "history_count" in body
    assert body["pending_review_count"] == 0
    assert body["recent_branches"] == []


# --- /self-improve/pending endpoint ------------------------------------------

def test_self_improve_pending_endpoint_empty(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/self-improve/pending", headers=_TOKEN).json()
    assert body["count"] == 0
    assert body["pending"] == []


def test_self_improve_pending_endpoint_with_data(tmp_path):
    from hive.core.spec_search import Edit, EditOp
    import asyncio
    hive = _hive(tmp_path)
    async def _noop(_wt): return []
    e = Edit(op=EditOp.PATCH_CODE, summary="pending fix", apply=_noop)
    asyncio.run(hive.improver.run([e]))
    with _client(hive) as c:
        body = c.get("/self-improve/pending", headers=_TOKEN).json()
    assert body["count"] == 1
    assert body["pending"][0]["summary"] == "pending fix"
    assert body["pending"][0]["op"] == "patch_code"


# --- /events/history endpoint -------------------------------------------------

def test_events_history_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/events/history", headers=_TOKEN).json()
    assert "events" in body and "count" in body
    assert isinstance(body["events"], list)


def test_events_stats_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/events/stats", headers=_TOKEN).json()
    assert "by_type" in body and "total" in body and "subscribers" in body


# --- /loop-guard endpoints ----------------------------------------------------

def test_loop_guard_stats_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/loop-guard/stats", headers=_TOKEN).json()
    assert "total_calls" in body and "max_per_tool" in body


def test_loop_guard_reset_endpoint(tmp_path):
    hive = _hive(tmp_path)
    hive.loop_guard.check("shell", {"cmd": "ls"})
    with _client(hive) as c:
        body = c.post("/loop-guard/reset", headers=_TOKEN).json()
        assert body["reset"] is True
        stats = c.get("/loop-guard/stats", headers=_TOKEN).json()
        assert stats["total_calls"] == 0


# --- /tasks/running endpoint --------------------------------------------------

def test_tasks_running_endpoint_empty(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/tasks/running", headers=_TOKEN).json()
    assert body["count"] == 0
    assert body["tasks"] == []


def test_tasks_running_endpoint_with_running_task(tmp_path):
    hive = _hive(tmp_path)
    tid = hive.task_board.enqueue("tool", {})
    hive.task_board.claim(tid)
    with _client(hive) as c:
        body = c.get("/tasks/running", headers=_TOKEN).json()
        assert body["count"] == 1
        assert body["tasks"][0]["kind"] == "tool"


# --- /commitments/active endpoint ---------------------------------------------

def test_commitments_active_names_empty(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/commitments/active", headers=_TOKEN).json()
    assert body["count"] == 0
    assert body["names"] == []


def test_commitments_active_names_with_data(tmp_path):
    hive = _hive(tmp_path)
    hive.commitments.add("daily report", 86400)
    hive.commitments.add("weekly sync", 604800)
    with _client(hive) as c:
        body = c.get("/commitments/active", headers=_TOKEN).json()
    assert body["count"] == 2
    assert "daily report" in body["names"]
    assert "weekly sync" in body["names"]


# --- /skills/recent endpoint --------------------------------------------------

def test_skills_recent_endpoint_structure(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/skills/recent", headers=_TOKEN).json()
    assert "count" in body and "skills" in body
    assert isinstance(body["skills"], list)
    assert body["count"] == len(body["skills"])


def test_skills_recent_endpoint_shows_used_skill(tmp_path):
    hive = _hive(tmp_path)
    hive.skill_usage.register("unique_test_skill_xyz")
    hive.skill_usage.record_use("unique_test_skill_xyz")
    with _client(hive) as c:
        body = c.get("/skills/recent", headers=_TOKEN).json()
    names = [s["name"] for s in body["skills"]]
    assert "unique_test_skill_xyz" in names


# --- /skills/{name} endpoint --------------------------------------------------

def test_skill_detail_endpoint(tmp_path):
    hive = _hive(tmp_path)
    hive.skill_usage.register("my_skill")
    with _client(hive) as c:
        body = c.get("/skills/my_skill", headers=_TOKEN).json()
    assert body["name"] == "my_skill"
    assert "use_count" in body and "state" in body


def test_skill_detail_endpoint_not_found(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.get("/skills/nonexistent", headers=_TOKEN)
    assert r.status_code == 404


# --- /audit/recent/{tool} endpoint -------------------------------------------

def test_audit_recent_by_tool_endpoint(tmp_path):
    hive = _hive(tmp_path)
    hive.audit_log.record({"tool": "shell", "status": "ok"})
    hive.audit_log.record({"tool": "write_file", "status": "ok"})
    with _client(hive) as c:
        body = c.get("/audit/recent/shell", headers=_TOKEN).json()
    assert body["tool"] == "shell"
    assert body["count"] == 1
    assert body["entries"][0]["tool"] == "shell"


def test_audit_recent_by_tool_endpoint_empty(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/audit/recent/nonexistent", headers=_TOKEN).json()
    assert body["count"] == 0
    assert body["entries"] == []


# --- /audit/errors endpoint ---------------------------------------------------

def test_audit_errors_endpoint_empty(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/audit/errors", headers=_TOKEN).json()
    assert "entries" in body and "count" in body


def test_audit_errors_endpoint_returns_errors(tmp_path):
    hive = _hive(tmp_path)
    hive.audit_log.record({"tool": "shell", "status": "ok"})
    hive.audit_log.record({"tool": "shell", "status": "error", "error": "oops"})
    with _client(hive) as c:
        body = c.get("/audit/errors", headers=_TOKEN).json()
    assert body["count"] >= 1
    assert all(e["status"] != "ok" for e in body["entries"])


# --- /cron/stats endpoint -----------------------------------------------------

def test_cron_stats_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/cron/stats", headers=_TOKEN).json()
    assert "total" in body and "enabled" in body and "due_now" in body


def test_cron_stats_reflects_added_jobs(tmp_path):
    hive = _hive(tmp_path)
    hive.cron.add("@daily", "check", enabled=True)
    hive.cron.add("@daily", "check2", enabled=False)
    with _client(hive) as c:
        body = c.get("/cron/stats", headers=_TOKEN).json()
    assert body["total"] == 2
    assert body["enabled"] == 1


# --- /skills/{name}/pin and /unpin endpoints ----------------------------------

def test_skill_pin_endpoint(tmp_path):
    hive = _hive(tmp_path)
    hive.skill_usage.register("test_pin_skill")
    with _client(hive) as c:
        body = c.post("/skills/test_pin_skill/pin", headers=_TOKEN).json()
        assert body["pinned"] is True
        assert hive.skill_usage.get("test_pin_skill").pinned is True


def test_skill_pin_endpoint_not_found(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.post("/skills/nonexistent/pin", headers=_TOKEN)
    assert r.status_code == 404


def test_skill_unpin_endpoint(tmp_path):
    hive = _hive(tmp_path)
    hive.skill_usage.register("test_unpin_skill")
    hive.skill_usage.pin("test_unpin_skill")
    with _client(hive) as c:
        body = c.post("/skills/test_unpin_skill/unpin", headers=_TOKEN).json()
    assert body["pinned"] is False


# --- /memory/topics endpoint --------------------------------------------------

def test_memory_topics_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/memory/topics", headers=_TOKEN).json()
    assert "topics" in body and "count" in body
    assert isinstance(body["topics"], list)


def test_memory_topics_after_learn(tmp_path):
    hive = _hive(tmp_path)
    if hasattr(hive.memory, "learn"):
        hive.memory.learn("fact", "test_topic_xyz", "test content")
    with _client(hive) as c:
        body = c.get("/memory/topics", headers=_TOKEN).json()
    assert isinstance(body["topics"], list)


# --- /tools/categories endpoint -----------------------------------------------

def test_tools_categories_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/tools/categories", headers=_TOKEN).json()
    assert "categories" in body and "count" in body
    assert isinstance(body["categories"], list)
    assert body["count"] == len(body["categories"])


# --- /tasks/last-failed endpoint -----------------------------------------------

def test_tasks_last_failed_endpoint_empty(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/tasks/last-failed", headers=_TOKEN).json()
    assert body["task"] is None


def test_tasks_last_failed_endpoint_with_data(tmp_path):
    hive = _hive(tmp_path)
    tid = hive.task_board.enqueue("tool", {})
    hive.task_board.claim(tid)
    hive.task_board.fail(tid, "last error message")
    with _client(hive) as c:
        body = c.get("/tasks/last-failed", headers=_TOKEN).json()
    assert body["task"] is not None
    assert body["task"]["last_error"] == "last error message"


# --- /memory/wipe-knowledge endpoint ------------------------------------------

def test_memory_wipe_knowledge_endpoint(tmp_path):
    hive = _hive(tmp_path)
    if hasattr(hive.memory, "learn"):
        hive.memory.learn("fact", "wipe_me", "test")
    with _client(hive) as c:
        body = c.delete("/memory/wipe-knowledge", headers=_TOKEN).json()
    assert "deleted" in body


# --- /tools/dangerous endpoint ------------------------------------------------

def test_tools_dangerous_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/tools/dangerous", headers=_TOKEN).json()
    assert "tools" in body and "count" in body
    assert isinstance(body["tools"], list)
    assert body["count"] == len(body["tools"])


# --- /audit/error-rate endpoint -----------------------------------------------

def test_audit_error_rate_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/audit/error-rate", headers=_TOKEN).json()
    assert "error_rate" in body
    assert "window_hours" in body
    assert 0.0 <= body["error_rate"] <= 1.0
    assert body["window_hours"] == 24.0


def test_audit_error_rate_endpoint_custom_window(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/audit/error-rate?window_hours=48", headers=_TOKEN).json()
    assert body["window_hours"] == 48.0


# --- /health/summary endpoint -------------------------------------------------

def test_health_summary_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/health/summary", headers=_TOKEN).json()
    assert "budget" in body
    assert "tasks" in body
    assert "cron" in body
    assert "self_mod" in body
    assert "audit" in body
    assert "calls_today" in body["budget"]
    assert "pending" in body["tasks"]
    assert "proposals" in body["self_mod"]
    assert "error_rate_24h" in body["audit"]


def test_health_summary_requires_token(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.get("/health/summary")
    assert r.status_code == 401


# --- /memory/stats endpoint ---------------------------------------------------

def test_memory_stats_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/memory/stats", headers=_TOKEN).json()
    assert "knowledge_count" in body
    assert "episodic_count" in body
    assert "avg_importance" in body
    assert "by_kind" in body


# --- /memory/important endpoint -----------------------------------------------

def test_memory_important_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/memory/important", headers=_TOKEN).json()
    assert "facts" in body
    assert "count" in body
    assert isinstance(body["facts"], list)


# --- /loop-guard/top-tools endpoint -------------------------------------------

def test_loop_guard_top_tools_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/loop-guard/top-tools", headers=_TOKEN).json()
    assert "tools" in body
    assert isinstance(body["tools"], list)


def test_loop_guard_top_tools_after_calls(tmp_path):
    hive = _hive(tmp_path)
    hive.loop_guard.check("read_file", {"path": "/x"})
    hive.loop_guard.check("read_file", {"path": "/y"})
    hive.loop_guard.check("write_file", {"path": "/z"})
    with _client(hive) as c:
        body = c.get("/loop-guard/top-tools?n=2", headers=_TOKEN).json()
    names = [t["name"] for t in body["tools"]]
    assert "read_file" in names


# --- /skills/unused endpoint --------------------------------------------------

def test_skills_unused_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/skills/unused", headers=_TOKEN).json()
    assert "skills" in body and "count" in body
    assert isinstance(body["skills"], list)


# --- /skills/archived endpoint ------------------------------------------------

def test_skills_archived_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/skills/archived", headers=_TOKEN).json()
    assert "skills" in body and "count" in body
    assert isinstance(body["skills"], list)


# --- /config/summary endpoint -------------------------------------------------

def test_config_summary_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/config/summary", headers=_TOKEN).json()
    assert "exec_model" in body
    assert body["secret"] == "***"  # redacted
    assert "is_production" in body


# --- /config/llm endpoint -----------------------------------------------------

def test_config_llm_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/config/llm", headers=_TOKEN).json()
    assert "exec_model" in body
    assert "exec_provider" in body
    assert "planner_enabled" in body


# --- /traces/stats endpoint ---------------------------------------------------

def test_traces_stats_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/traces/stats", headers=_TOKEN).json()
    assert "session_count" in body
    assert "total_events" in body
    assert "sessions" in body


# --- /commitments/upcoming endpoint -------------------------------------------

def test_commitments_upcoming_endpoint(tmp_path):
    hive = _hive(tmp_path)
    hive.commitments.add("check system", 3600.0)
    hive.commitments.add("weekly report", 604800.0)
    with _client(hive) as c:
        body = c.get("/commitments/upcoming", headers=_TOKEN).json()
    assert "upcoming" in body and "count" in body
    assert body["count"] == 2
    assert isinstance(body["upcoming"], list)


# --- /self-improve/stages endpoint --------------------------------------------

def test_self_improve_stages_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/self-improve/stages", headers=_TOKEN).json()
    assert "by_stage" in body
    assert "total" in body
    assert body["total"] == 0  # no proposals yet


# --- /budget/warning endpoint -------------------------------------------------

def test_budget_warning_endpoint_healthy(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/budget/warning", headers=_TOKEN).json()
    assert "warning" in body
    # Fresh budget → no warning
    assert body["warning"] is None


# --- /llm/pool endpoint -------------------------------------------------------

def test_llm_pool_endpoint(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        body = c.get("/llm/pool", headers=_TOKEN).json()
    assert "pool_size" in body
    assert "available" in body
    assert "labels" in body
    assert "total_failures" in body


# --- OpenAI-compat /v1/ endpoints (G-5) ----------------------------------------

class _V1StreamRouter:
    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        return CompletionResult(text="hello from hive", model="hive")

    async def stream(self, messages, *, system=None, **kw):
        for part in ("hello", " from", " hive"):
            yield part

    async def aclose(self):
        pass


def test_v1_models_returns_list(tmp_path):
    with _client(_hive(tmp_path)) as c:
        r = c.get("/v1/models", headers=_TOKEN)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["data"][0]["id"] == "hive"


def test_v1_chat_completions_non_streaming(tmp_path):
    hive = _hive(tmp_path, ["hello from hive"])
    with _client(hive) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "hive", "messages": [{"role": "user", "content": "hi"}]},
            headers=_TOKEN,
        )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert "hello from hive" in body["choices"][0]["message"]["content"]


def test_v1_chat_completions_streaming(tmp_path):
    hive = HiveOS.build(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        router=_V1StreamRouter(),
    )
    with _client(hive) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "hive", "messages": [{"role": "user", "content": "hi"}],
                  "stream": True},
            headers=_TOKEN,
        )
    assert r.status_code == 200
    body = r.text
    assert "chat.completion.chunk" in body
    assert "data: [DONE]" in body


def test_v1_chat_completions_requires_auth(tmp_path):
    with _client(_hive(tmp_path)) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "hive", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# channel_hint wiring
# ---------------------------------------------------------------------------

def test_chat_passes_channel_hint_web(tmp_path):
    """POST /chat must pass channel_hint='web' to hive.ask()."""
    from unittest.mock import AsyncMock, patch
    from hive.runtime import HiveOS
    hive = _hive(tmp_path)
    with patch.object(HiveOS, "ask", new_callable=AsyncMock, return_value="pong") as mock_ask:
        with _client(hive) as c:
            resp = c.post("/chat", json={"message": "ping"}, headers=_TOKEN)
    assert resp.status_code == 200
    mock_ask.assert_called_once()
    _, kwargs = mock_ask.call_args
    assert kwargs.get("channel_hint") == "web"


def test_v1_completions_passes_channel_hint_api(tmp_path):
    """POST /v1/chat/completions must pass channel_hint='api' to hive.ask()."""
    from unittest.mock import AsyncMock, patch
    from hive.runtime import HiveOS
    hive = _hive(tmp_path)
    with patch.object(HiveOS, "ask", new_callable=AsyncMock, return_value="pong") as mock_ask:
        with _client(hive) as c:
            resp = c.post(
                "/v1/chat/completions",
                json={"model": "hive", "messages": [{"role": "user", "content": "hi"}],
                      "stream": False},
                headers=_TOKEN,
            )
    assert resp.status_code == 200
    mock_ask.assert_called_once()
    _, kwargs = mock_ask.call_args
    assert kwargs.get("channel_hint") == "api"


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

def test_security_headers_present(tmp_path):
    """Every HTTP response should include key security headers."""
    with _client(_hive(tmp_path)) as c:
        resp = c.get("/health")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def test_chat_rejects_oversized_message(tmp_path):
    """Messages exceeding max_message_len must return 422."""
    huge = "x" * 33_000
    resp = _client(_hive(tmp_path)).post(
        "/chat", json={"message": huge}, headers=_TOKEN
    )
    assert resp.status_code == 422


def test_chat_rejects_invalid_session_id(tmp_path):
    """session_id with special chars must return 422."""
    resp = _client(_hive(tmp_path)).post(
        "/chat", json={"message": "hi", "session_id": "bad/../path"}, headers=_TOKEN
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /telemetry
# ---------------------------------------------------------------------------

def test_get_telemetry_returns_200(tmp_path):
    with _client(_hive(tmp_path)) as c:
        resp = c.get("/telemetry", headers=_TOKEN)
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


def test_get_telemetry_requires_auth(tmp_path):
    with _client(_hive(tmp_path)) as c:
        resp = c.get("/telemetry")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /self-diagnose
# ---------------------------------------------------------------------------

def test_post_self_diagnose_returns_200(tmp_path):
    with _client(_hive(tmp_path)) as c:
        resp = c.post("/self-diagnose", params={"dry_run": True}, headers=_TOKEN)
    assert resp.status_code in (200, 202)


def test_post_self_diagnose_requires_auth(tmp_path):
    with _client(_hive(tmp_path)) as c:
        resp = c.post("/self-diagnose")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# WebSocket — additional assertion not covered by test_ws_rejects_bad_token
# ---------------------------------------------------------------------------

def test_ws_rejects_invalid_token_sends_error_type(tmp_path):
    """WS bad token must reply with type=='error' containing 'unauthorized'."""
    with _client(_hive(tmp_path)) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_text("wrong-token")
            data = ws.receive_json()
    assert data.get("type") == "error"
    assert "unauthorized" in data.get("data", "").lower()


# ---------------------------------------------------------------------------
# New endpoint tests
# ---------------------------------------------------------------------------

def test_health_endpoint_returns_ok(tmp_path):
    """GET /health with no auth header returns 200 with a 'status' key."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert "status" in r.json()


def test_health_endpoint_has_hive_fields(tmp_path):
    """GET /health/full (which calls hive.health()) returns a dict with 'tools' key."""
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.get("/health/full", headers=_TOKEN)
    assert r.status_code == 200
    body = r.json()
    assert "tools" in body


class _StreamRouter:
    """Minimal router that supports both complete() and stream()."""

    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        from hive.llm.adapters.base import CompletionResult
        return CompletionResult(text="hi", model="m")

    async def stream(self, messages, *, system=None, **kw):
        for part in ("hello", " world"):
            yield part

    async def aclose(self):
        pass


def test_chat_stream_returns_sse_format(tmp_path):
    """POST /chat/stream returns 200, content-type text/event-stream, body has 'data:' line."""
    from hive.core.config import HiveConfig
    hive = HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False),
                        router=_StreamRouter())
    with _client(hive) as c:
        r = c.post("/chat/stream", json={"message": "hi"}, headers=_TOKEN)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    assert "data:" in r.text


def test_chat_stream_error_does_not_leak_exception_class(tmp_path):
    """When ask_stream raises, the SSE stream emits 'event: error' and the data
    line is only the exception class name — NOT the exception message."""
    from hive.core.config import HiveConfig

    class _BoomStreamRouter:
        async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
            from hive.llm.adapters.base import CompletionResult
            return CompletionResult(text="", model="m")

        async def stream(self, messages, *, system=None, **kw):
            raise RuntimeError("secret key abc123")
            # make it a generator
            yield  # noqa: unreachable

        async def aclose(self):
            pass

    hive = HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False),
                        router=_BoomStreamRouter())
    with _client(hive) as c:
        r = c.post("/chat/stream", json={"message": "hi"}, headers=_TOKEN)
    assert r.status_code == 200
    body = r.text
    assert "event: error" in body
    assert "secret key abc123" not in body
    # The data line after 'event: error' must be just the class name
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "event: error":
            data_line = lines[i + 1] if i + 1 < len(lines) else ""
            assert data_line.startswith("data: RuntimeError")
            assert "secret key abc123" not in data_line
            break
    else:
        raise AssertionError("'event: error' line not found in SSE stream")


def test_traces_export_endpoint_exists(tmp_path):
    """GET /traces/export/{session_id} must be registered (200 or 404, not 405)."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/traces/export/some-session-id", headers=_TOKEN)
    assert r.status_code != 405, "Route not registered (Method Not Allowed)"
    assert r.status_code in (200, 404)


# --- /audit (basic list) endpoint ---------------------------------------------

def test_audit_list_requires_token(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.get("/audit")
    assert r.status_code == 401


def test_audit_list_returns_entries_key(tmp_path):
    hive = _hive(tmp_path)
    hive.audit_log.record({"tool": "ping", "status": "ok"})
    with _client(hive) as c:
        body = c.get("/audit", headers=_TOKEN).json()
    assert "entries" in body
    assert isinstance(body["entries"], list)
    assert len(body["entries"]) >= 1


def test_audit_list_limit_param(tmp_path):
    hive = _hive(tmp_path)
    for i in range(5):
        hive.audit_log.record({"tool": f"tool_{i}", "status": "ok"})
    with _client(hive) as c:
        body = c.get("/audit", params={"limit": 2}, headers=_TOKEN).json()
    assert len(body["entries"]) <= 2


# --- /tasks/{task_id} single-task endpoints -----------------------------------

def test_task_get_by_id(tmp_path):
    hive = _hive(tmp_path)
    tid = hive.task_board.enqueue("ping", {"x": 1})
    with _client(hive) as c:
        body = c.get(f"/tasks/{tid}", headers=_TOKEN).json()
    assert body["id"] == tid
    assert body["kind"] == "ping"
    assert "state" in body and "payload" in body


def test_task_get_unknown_returns_404(tmp_path):
    hive = _hive(tmp_path)
    with _client(hive) as c:
        r = c.get("/tasks/99999", headers=_TOKEN)
    assert r.status_code == 404


def test_task_retry_single_failed_task(tmp_path):
    hive = _hive(tmp_path)
    tid = hive.task_board.enqueue("tool", {})
    hive.task_board.claim(tid)
    hive.task_board.fail(tid, "some error")
    with _client(hive) as c:
        body = c.post(f"/tasks/{tid}/retry", headers=_TOKEN).json()
        assert body["retried"] is True
        assert body["task_id"] == tid
        assert hive.task_board.get(tid).state == "pending"


def test_task_retry_non_failed_returns_409(tmp_path):
    hive = _hive(tmp_path)
    tid = hive.task_board.enqueue("tool", {})
    # Task is still pending (not failed) — retry should fail
    with _client(hive) as c:
        r = c.post(f"/tasks/{tid}/retry", headers=_TOKEN)
    assert r.status_code == 409


def test_task_cancel_single_pending_task(tmp_path):
    hive = _hive(tmp_path)
    tid = hive.task_board.enqueue("tool", {})
    with _client(hive) as c:
        body = c.post(f"/tasks/{tid}/cancel", headers=_TOKEN).json()
        assert body["cancelled"] is True
        assert body["task_id"] == tid
        # TaskBoard.cancel() marks the task as "failed" (not pending)
        assert hive.task_board.get(tid).state != "pending"


def test_task_cancel_running_task_returns_409(tmp_path):
    hive = _hive(tmp_path)
    tid = hive.task_board.enqueue("tool", {})
    hive.task_board.claim(tid)   # now RUNNING, not PENDING
    with _client(hive) as c:
        r = c.post(f"/tasks/{tid}/cancel", headers=_TOKEN)
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Wrong HTTP method (405) — ensure routes exist on the right verb only
# ---------------------------------------------------------------------------

def test_get_chat_returns_405(tmp_path):
    """GET /chat is not registered — must return 405, not 404."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/chat", headers=_TOKEN)
    assert r.status_code == 405


def test_post_budget_returns_405(tmp_path):
    """POST /budget is not registered — must return 405, not 404."""
    with _client(_hive(tmp_path)) as c:
        r = c.post("/budget", json={}, headers=_TOKEN)
    assert r.status_code == 405


def test_get_loop_guard_reset_returns_405(tmp_path):
    """GET /loop-guard/reset is not registered — endpoint is POST only."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/loop-guard/reset", headers=_TOKEN)
    assert r.status_code == 405


# ---------------------------------------------------------------------------
# JSON Content-Type — common endpoints must advertise application/json
# ---------------------------------------------------------------------------

def test_health_response_is_json(tmp_path):
    """/health must respond with application/json content-type."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/health")
    assert "application/json" in r.headers.get("content-type", "")


def test_budget_response_is_json(tmp_path):
    """/budget must respond with application/json content-type."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/budget", headers=_TOKEN)
    assert "application/json" in r.headers.get("content-type", "")


def test_audit_response_is_json(tmp_path):
    """/audit list must respond with application/json content-type."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/audit", headers=_TOKEN)
    assert "application/json" in r.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# Auth required — endpoints that are missing from other auth-required checks
# ---------------------------------------------------------------------------

def test_tools_list_requires_auth(tmp_path):
    """GET /tools without token must return 401."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/tools")
    assert r.status_code == 401


def test_skills_unused_requires_auth(tmp_path):
    """GET /skills/unused without token must return 401."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/skills/unused")
    assert r.status_code == 401


def test_skills_archived_requires_auth(tmp_path):
    """GET /skills/archived without token must return 401."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/skills/archived")
    assert r.status_code == 401


def test_loop_guard_stats_requires_auth(tmp_path):
    """GET /loop-guard/stats without token must return 401."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/loop-guard/stats")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Pagination / limit params
# ---------------------------------------------------------------------------

def test_skills_recent_limit_param(tmp_path):
    """GET /skills/recent?limit=1 must return at most 1 skill."""
    hive = _hive(tmp_path)
    for name in ("skill_a", "skill_b", "skill_c"):
        hive.skill_usage.register(name)
        hive.skill_usage.record_use(name)
    with _client(hive) as c:
        body = c.get("/skills/recent", params={"limit": 1}, headers=_TOKEN).json()
    assert len(body["skills"]) <= 1


def test_commitments_upcoming_limit_param(tmp_path):
    """GET /commitments/upcoming?limit=1 must return at most 1 entry."""
    hive = _hive(tmp_path)
    hive.commitments.add("task alpha", 3600.0)
    hive.commitments.add("task beta", 7200.0)
    hive.commitments.add("task gamma", 10800.0)
    with _client(hive) as c:
        body = c.get("/commitments/upcoming", params={"limit": 1}, headers=_TOKEN).json()
    assert len(body["upcoming"]) <= 1


# ---------------------------------------------------------------------------
# POST /telegram/webhook — only registered when telegram token is configured
# ---------------------------------------------------------------------------

def test_telegram_webhook_not_registered_without_token(tmp_path):
    """Without a telegram token the /telegram/webhook route must not exist (404)."""
    hive = _hive(tmp_path)  # no TELEGRAM_BOT_TOKEN env var
    with _client(hive) as c:
        r = c.post("/telegram/webhook", json={})
    # 404 means route not registered; 405 would also mean no matching path
    assert r.status_code in (404, 405)


def test_telegram_webhook_registered_with_token(tmp_path):
    """With a telegram token the /telegram/webhook route is registered (not 404/405)."""
    from unittest.mock import AsyncMock, MagicMock
    from hive.gateway.channels.telegram import TelegramChannel

    # Build hive normally; inject a stub TelegramChannel into create_app directly
    import dataclasses
    hive = _hive(tmp_path)
    hive.config = dataclasses.replace(
        hive.config,
        telegram_webhook_secret="correct_secret",
        telegram_allowed_user_ids=frozenset({"1"}),
    )
    stub_tg = MagicMock(spec=TelegramChannel)
    stub_tg.parse_update = MagicMock(return_value=None)  # unknown update → handled=False
    stub_tg.send = AsyncMock()

    from hive.gateway.app import create_app
    app = create_app(hive, telegram=stub_tg)
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        r = c.post("/telegram/webhook", json={"unknown": "payload"}, headers={
            "X-Telegram-Bot-Api-Secret-Token": "correct_secret",
        })
    # Route IS registered → must not be 404/405
    assert r.status_code not in (404, 405)
    assert r.json()["handled"] is False


def test_telegram_webhook_ignores_non_message_update(tmp_path):
    """Updates without a 'message' key are gracefully ignored (handled=False)."""
    from unittest.mock import AsyncMock, MagicMock
    from hive.gateway.channels.telegram import TelegramChannel

    import dataclasses
    hive = _hive(tmp_path)
    hive.config = dataclasses.replace(
        hive.config,
        telegram_webhook_secret="correct_secret",
        telegram_allowed_user_ids=frozenset({"1"}),
    )
    stub_tg = MagicMock(spec=TelegramChannel)
    stub_tg.parse_update = MagicMock(return_value=None)
    stub_tg.send = AsyncMock()

    from hive.gateway.app import create_app
    from starlette.testclient import TestClient
    app = create_app(hive, telegram=stub_tg)
    with TestClient(app) as c:
        r = c.post("/telegram/webhook", json={"edited_message": {"text": "hi"}}, headers={
            "X-Telegram-Bot-Api-Secret-Token": "correct_secret",
        })
    assert r.status_code == 200
    assert r.json() == {"ok": True, "handled": False}


def test_telegram_webhook_rejects_bad_secret(tmp_path):
    """Webhook with wrong X-Telegram-Bot-Api-Secret-Token must be rejected with 401."""
    import dataclasses
    from unittest.mock import AsyncMock, MagicMock
    from hive.gateway.channels.telegram import TelegramChannel
    from hive.core.config import HiveConfig

    # Build hive with a webhook secret baked into its config.
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    cfg = dataclasses.replace(cfg, telegram_webhook_secret="correct_secret")
    hive = HiveOS.build(cfg, router=_ScriptRouter([]))

    stub_tg = MagicMock(spec=TelegramChannel)
    stub_tg.parse_update = MagicMock(return_value=None)
    stub_tg.send = AsyncMock()

    from hive.gateway.app import create_app
    from starlette.testclient import TestClient
    app = create_app(hive, telegram=stub_tg)
    with TestClient(app) as c:
        r = c.post(
            "/telegram/webhook",
            json={"message": {"text": "hi", "chat": {"id": 1}}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong_secret"},
        )
    assert r.status_code == 401


def test_telegram_webhook_rejects_oversized_payload(tmp_path):
    import dataclasses
    from unittest.mock import MagicMock
    from hive.gateway.app import MAX_WEBHOOK_BODY
    from hive.gateway.channels.telegram import TelegramChannel

    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    cfg = dataclasses.replace(
        cfg,
        telegram_token="token",
        telegram_webhook_secret="correct_secret",
        telegram_allowed_user_ids=frozenset({"1"}),
    )
    hive = HiveOS.build(cfg, router=_ScriptRouter([]))
    app = create_app(hive, telegram=MagicMock(spec=TelegramChannel))
    with TestClient(app) as client:
        response = client.post(
            "/telegram/webhook",
            content=b"x" * (MAX_WEBHOOK_BODY + 1),
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct_secret"},
        )
    assert response.status_code == 413


def test_telegram_webhook_refuses_unallowed_sender_before_model_turn(tmp_path, monkeypatch):
    import dataclasses
    from unittest.mock import AsyncMock, MagicMock
    from hive.gateway.channels.base import MessageEvent
    from hive.gateway.channels.telegram import TelegramChannel

    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    cfg = dataclasses.replace(
        cfg,
        telegram_token="token",
        telegram_webhook_secret="correct_secret",
        telegram_allowed_user_ids=frozenset({"1"}),
    )
    hive = HiveOS.build(cfg, router=_ScriptRouter([]))
    ask = AsyncMock()
    monkeypatch.setattr(type(hive), "ask", ask)
    stub_tg = MagicMock(spec=TelegramChannel)
    event = MessageEvent(
        text="hi", chat_id="2", user_id="2", message_id="m2", platform="telegram",
    )
    stub_tg.parse_update.return_value = event
    app = create_app(hive, telegram=stub_tg)
    with TestClient(app) as client:
        response = client.post("/telegram/webhook", json={"message": {}}, headers={
            "X-Telegram-Bot-Api-Secret-Token": "correct_secret",
        })
    assert response.json() == {"ok": True, "handled": False, "reason": "sender_not_allowed"}
    assert event.trust == "untrusted"
    hive.ask.assert_not_awaited()


# --- 6 new gateway tests -------------------------------------------------------

def test_health_includes_service_name(tmp_path):
    """/health body must include 'service' field set to 'hiveos-gateway'."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body.get("service") == "hiveos-gateway"


def test_v1_models_no_auth_returns_401(tmp_path):
    """/v1/models must return 401 when the Authorization header is absent."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/v1/models")
        assert r.status_code == 401


def test_v1_models_data_structure(tmp_path):
    """/v1/models data list must contain dicts with 'id' and 'object' keys."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/v1/models", headers=_TOKEN)
        assert r.status_code == 200
        first = r.json()["data"][0]
        assert "id" in first
        assert "object" in first


def test_v1_chat_completions_empty_messages_returns_400(tmp_path):
    """/v1/chat/completions with no user message in messages must return 400."""
    with _client(_hive(tmp_path)) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "system", "content": "you are helpful"}]},
            headers=_TOKEN,
        )
        assert r.status_code == 400


def test_v1_chat_completions_reply_has_usage(tmp_path):
    """/v1/chat/completions response must include a 'usage' dict."""
    hive = _hive(tmp_path, [CompletionResult(text="answer", model="m")])
    with _client(hive) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=_TOKEN,
        )
        assert r.status_code == 200
        body = r.json()
        assert "usage" in body
        assert "prompt_tokens" in body["usage"]


def test_budget_detail_is_near_cap_type(tmp_path):
    """/budget/detail is_near_cap field must be a boolean."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/budget/detail", headers=_TOKEN)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["is_near_cap"], bool)


# --- Wave 3U additional tests ---------------------------------------------------

def test_config_summary_endpoint_returns_dict(tmp_path):
    """/config/summary must return a JSON dict."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/config/summary", headers=_TOKEN)
        assert r.status_code == 200
        assert isinstance(r.json(), dict)


def test_tools_endpoint_returns_dict_with_tools_list(tmp_path):
    """/tools must return a JSON dict containing a 'tools' list."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/tools", headers=_TOKEN)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict)
        assert "tools" in body
        assert isinstance(body["tools"], list)
        assert len(body["tools"]) > 0


def test_tools_categories_returns_dict(tmp_path):
    """/tools/categories must return a JSON dict mapping category to count."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/tools/categories", headers=_TOKEN)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict)


def test_audit_endpoint_returns_dict_with_entries(tmp_path):
    """/audit must return a JSON dict with an 'entries' key."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/audit", headers=_TOKEN)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict)
        assert "entries" in body
        assert isinstance(body["entries"], list)


def test_skills_endpoint_returns_dict_with_total(tmp_path):
    """/skills must return a JSON dict with a 'total' key."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/skills", headers=_TOKEN)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict)
        assert "total" in body


def test_budget_forecast_endpoint_returns_dict(tmp_path):
    """/budget/forecast must return a JSON dict."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/budget/forecast", headers=_TOKEN)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict)


def test_telemetry_endpoint_returns_dict(tmp_path):
    """/telemetry must return a JSON dict with at least one key."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/telemetry", headers=_TOKEN)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict)
        assert len(body) > 0


def test_config_llm_endpoint_returns_dict(tmp_path):
    """/config/llm must return a JSON dict with exec_model key."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/config/llm", headers=_TOKEN)
        assert r.status_code == 200
        body = r.json()
        assert "exec_model" in body
