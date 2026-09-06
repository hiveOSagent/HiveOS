"""Coverage batch — tools/discovery + tools/builtins follow-up.

Target modules:
  src/hive/tools/discovery.py         73% → 100% (14 missed lines closed)
  src/hive/tools/builtins/__init__.py 94% → 100% (31 missed lines closed)

What this file covers:

  tools/discovery.py
    - line 62    : github_token supplied → Authorization Bearer header
    - lines 70-71: MCP registry source raising during search
    - lines 77-81: GitHub source happy path (items returned) + source raising
    - line 93    : memory.learn() called after a fresh discovery (cache miss)
    - lines 100-106: audit_repo happy path (no red flags), audit_repo on
                    HTTP exception (safe=None, error=...)

  tools/builtins/__init__.py
    - lines 137-144: WebGet.execute() happy path (valid URL → response.text)
    - lines 272-275: Deploy._run_cmd() timeout branch (kill + drain on TimeoutError)
    - lines 466-468: DiscoverTool with enable_security_audit=True (delegate_named)
    - lines 648-660: GitHubListPRs.execute() happy path (mocked _get)
    - lines 712-729: GitHubListCommits.execute() happy path (mocked _get)

All tests are offline (httpx.AsyncClient monkey-patched, no real network).
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from hive.core.types import ToolResult
from hive.tools.builtins import (
    Deploy,
    DiscoverTool,
    GitHubListCommits,
    GitHubListPRs,
    WebGet,
)
from hive.tools.discovery import audit_repo, discover, scan_red_flags


# ===========================================================================
# tools/discovery.py
# ===========================================================================

class _FakeResponse:
    """Minimal stand-in for httpx.Response — only .json() and .text."""

    def __init__(self, *, json_payload=None, text=""):
        self._json = json_payload if json_payload is not None else {}
        self.text = text

    def json(self):
        return self._json


def _patch_async_client(monkeypatch, *, mcp_payload=None, mcp_raises=None,
                        github_payload=None, github_raises=None):
    """Replace httpx.AsyncClient with a fake that returns the given payloads.

    Each of (mcp, github) is either a payload dict (returned via .json()) or
    an Exception class/instance (raised when .get() is called)."""
    captured: dict[str, dict] = {"calls": []}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self._headers = kwargs.get("headers", {})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, **kwargs):
            captured["calls"].append({"url": url, "kwargs": kwargs,
                                      "headers": self._headers})
            if "modelcontextprotocol" in url:
                if mcp_raises is not None:
                    raise mcp_raises() if isinstance(mcp_raises, type) else mcp_raises
                return _FakeResponse(json_payload=mcp_payload or {})
            if mcp_raises is None and github_payload is None and github_raises is None:
                # Convenience: caller passed only mcp_payload, so we still need
                # to answer the github source — return empty items.
                return _FakeResponse(json_payload={"items": []})
            if github_raises is not None:
                raise github_raises() if isinstance(github_raises, type) else github_raises
            return _FakeResponse(json_payload=github_payload or {"items": []})

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    return captured


def test_discover_passes_bearer_token_when_github_token_supplied(monkeypatch):
    """When github_token is set, discover() sends Authorization: Bearer ... (line 62)."""
    captured = _patch_async_client(monkeypatch,
                                   mcp_payload={"servers": []},
                                   github_payload={"items": []})
    asyncio.run(discover("anything", github_token="gh_secret_xyz"))
    # The github call must carry the bearer header.
    gh_calls = [c for c in captured["calls"] if "api.github.com" in c["url"]]
    assert gh_calls, "expected a GitHub source call"
    assert gh_calls[0]["headers"].get("Authorization") == "Bearer gh_secret_xyz"


def test_discover_no_auth_header_when_no_token(monkeypatch):
    """Without github_token, the Authorization header is absent."""
    captured = _patch_async_client(monkeypatch,
                                   mcp_payload={"servers": []},
                                   github_payload={"items": []})
    asyncio.run(discover("anything"))
    gh_calls = [c for c in captured["calls"] if "api.github.com" in c["url"]]
    assert gh_calls[0]["headers"].get("Authorization") is None


def test_discover_logs_and_continues_when_mcp_registry_fails(monkeypatch, caplog):
    """MCP registry exception is caught and logged (lines 70-71) — discovery continues."""
    import logging
    captured = _patch_async_client(monkeypatch,
                                   mcp_raises=RuntimeError("registry down"),
                                   github_payload={"items": []})
    with caplog.at_level(logging.DEBUG, logger="hive.tools.discovery"):
        out = asyncio.run(discover("anything"))
    # The github source still ran; mcp's failure was swallowed.
    assert any("api.github.com" in c["url"] for c in captured["calls"])
    assert "mcp registry search failed" in caplog.text


def test_discover_logs_and_continues_when_github_source_fails(monkeypatch, caplog):
    """GitHub source exception is caught and logged (lines 80-81)."""
    import logging
    captured = _patch_async_client(monkeypatch,
                                   mcp_payload={"servers": []},
                                   github_raises=RuntimeError("gh down"))
    with caplog.at_level(logging.DEBUG, logger="hive.tools.discovery"):
        out = asyncio.run(discover("anything"))
    # Result still surfaces (cached=False, possibly empty candidates list).
    assert out["cached"] is False
    assert "github search failed" in caplog.text


def test_discover_collects_github_items_into_candidates(monkeypatch):
    """GitHub items become candidates with stars (lines 76-79)."""
    _patch_async_client(
        monkeypatch,
        mcp_payload={"servers": []},
        github_payload={"items": [
            {"full_name": "owner/repo-a", "html_url": "https://github.com/owner/repo-a",
             "stargazers_count": 42},
            {"full_name": "owner/repo-b", "html_url": "https://github.com/owner/repo-b",
             "stargazers_count": 7},
        ]},
    )
    out = asyncio.run(discover("anything", limit=5))
    names = {c["name"] for c in out["candidates"] if c.get("source") == "github"}
    assert "owner/repo-a" in names
    assert "owner/repo-b" in names
    # stars are preserved
    star = next(c for c in out["candidates"] if c["name"] == "owner/repo-a")
    assert star["stars"] == 42


def test_discover_calls_memory_learn_after_fresh_discovery(monkeypatch):
    """When memory is supplied but recall() misses, memory.learn() is invoked (line 93)."""
    _patch_async_client(monkeypatch,
                        mcp_payload={"servers": []},
                        github_payload={"items": []})
    learned: list = []

    class _Mem:
        def recall(self, query, limit=5):
            return []  # miss → discovery proceeds
        def learn(self, kind, topic, content, source=""):
            learned.append((kind, topic, content, source))

    asyncio.run(discover("mcp servers", memory=_Mem()))
    assert learned, "expected memory.learn() to be called on a fresh discovery"
    kind, topic, _content, source = learned[0]
    assert kind == "research"
    assert topic == "discovery mcp servers"
    assert source == "discovery-engine"


def test_audit_repo_clean_url_returns_safe():
    """audit_repo on a clean URL returns safe=True, verdict=looks-clean (lines 100-106)."""
    captured = {}

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url):
            captured["url"] = url
            return _FakeResponse(text="def hello(): return 42")

    import httpx as _httpx
    import unittest.mock
    with unittest.mock.patch.object(_httpx, "AsyncClient", _Client):
        out = asyncio.run(audit_repo("https://example.com/clean.py"))
    assert out["safe"] is True
    assert out["flags"] == []
    assert out["verdict"] == "looks-clean"
    assert captured["url"] == "https://example.com/clean.py"


def test_audit_repo_flags_dangerous_url():
    """audit_repo with a red-flagged URL returns safe=False, verdict=review-needed."""
    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url):
            return _FakeResponse(text="os.system('rm -rf /')")

    import httpx as _httpx
    import unittest.mock
    with unittest.mock.patch.object(_httpx, "AsyncClient", _Client):
        out = asyncio.run(audit_repo("https://example.com/evil.py"))
    assert out["safe"] is False
    assert "rm -rf" in out["flags"]
    assert out["verdict"] == "review-needed"


def test_audit_repo_returns_safe_none_when_http_fails():
    """audit_repo on HTTP exception returns safe=None, error=msg (lines 104-105)."""
    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url):
            raise RuntimeError("connection refused")

    import httpx as _httpx
    import unittest.mock
    with unittest.mock.patch.object(_httpx, "AsyncClient", _Client):
        out = asyncio.run(audit_repo("https://unreachable.example/"))
    assert out["safe"] is None
    assert "connection refused" in out["error"]


# ===========================================================================
# tools/builtins/__init__.py
# ===========================================================================

def test_web_get_returns_response_text_on_valid_url():
    """WebGet.execute() streams a valid response and returns its text."""
    tool = WebGet()

    class _Resp:
        is_success = True
        encoding = "utf-8"

        async def aiter_bytes(self, chunk_size):
            assert chunk_size == 12_000
            yield b"hello from the web"

    class _Stream:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a):
            return None

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        def stream(self, method, url):
            assert method == "GET"
            assert url.startswith("http")
            return _Stream()

    import httpx as _httpx
    import unittest.mock
    with unittest.mock.patch.object(_httpx, "AsyncClient", _Client):
        out = asyncio.run(tool.execute(url="https://example.com/"))
    assert out.tool_name == "web_get"
    assert "hello from the web" in out.content
    assert out.success is True


def test_web_get_returns_failure_on_http_error_status():
    """WebGet surfaces is_success=False when the response is an HTTP error."""
    tool = WebGet()

    class _Resp:
        is_success = False
        encoding = "utf-8"

        async def aiter_bytes(self, chunk_size):
            yield b"forbidden"

    class _Stream:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a):
            return None

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        def stream(self, method, url):
            assert method == "GET"
            return _Stream()

    import httpx as _httpx
    import unittest.mock
    with unittest.mock.patch.object(_httpx, "AsyncClient", _Client):
        out = asyncio.run(tool.execute(url="https://example.com/secret"))
    assert out.success is False
    assert "forbidden" in out.content


def test_deploy_run_cmd_timeout_kills_subprocess():
    """Deploy._run_cmd() timeout branch (lines 272-275): kills subprocess and returns timeout result."""
    killed: list = []

    class _FakeProc:
        returncode = None

        async def communicate(self):
            # The fake wait_for below cancels the coroutine before it's ever
            # awaited the first time, so the FIRST call into communicate is
            # actually the drain-after-kill call. Just return empty bytes.
            return (b"", b"")

        def kill(self):
            killed.append(True)

    d = Deploy()

    async def fake_shell(cmd, **kw):
        return _FakeProc()

    import asyncio as _aio
    orig_shell = _aio.create_subprocess_shell
    orig_wait_for = _aio.wait_for
    _aio.create_subprocess_shell = fake_shell

    async def fake_wait_for(awaitable, timeout):
        # Cancel the inner coroutine so it's never executed (mimics a real
        # wait_for timeout) and surface the same exception type.
        awaitable.close()
        raise _aio.TimeoutError()

    _aio.wait_for = fake_wait_for
    try:
        out = asyncio.run(d._run_cmd("systemctl restart hiveos-gateway", timeout=0.5))
    finally:
        _aio.create_subprocess_shell = orig_shell
        _aio.wait_for = orig_wait_for

    assert killed, "subprocess.kill() must be called on timeout"
    assert out.tool_name == "deploy"
    assert out.success is False
    assert "timeout after 0.5s" in out.content


def test_discover_tool_with_security_audit_uses_delegate(monkeypatch):
    """DiscoverTool with enable_security_audit=True calls delegate_named (lines 466-468)."""
    captured: dict = {}

    async def fake_delegate_named(tasks, name):
        captured["tasks"] = tasks
        captured["name"] = name
        return [MagicMock(content=f"audit: {name}")]

    # Avoid the real agent system entirely.
    monkeypatch.setattr("hive.agents.delegate.delegate_named", fake_delegate_named)
    # The tool's discover() call would normally hit the network — stub it.
    async def fake_discover(need, *, memory=None, github_token="", limit=5,
                            security_delegate=None):
        # Exercise the security_delegate with a candidate URL so the inner
        # async function actually runs.
        note = await security_delegate("Audit some-tool at https://example.com/x")
        return {"need": need, "cached": False,
                "candidates": [{"name": "some-tool", "url": "https://example.com/x",
                                "security_note": note}]}

    monkeypatch.setattr("hive.tools.builtins._discovery.discover", fake_discover)
    # P-H: DiscoverTool now checks the local AST index first; force the
    # fast-path to miss so the web-fallback branch runs.
    monkeypatch.setattr("hive.tools.builtins._introspect.search", lambda *a, **k: [])

    d = DiscoverTool(enable_security_audit=True)
    out = asyncio.run(d.execute(need="some-tool"))
    assert out.tool_name == "discover"
    # delegate_named received the audit task and the security-reviewer name.
    assert captured["name"] == "security-reviewer"
    assert any("Audit some-tool" in t for t in captured["tasks"])


def test_discover_tool_uses_ast_fast_path_when_score_above_threshold(monkeypatch):
    """DiscoverTool short-circuits to AST results when local score >= 0.8 (lines 469-472).

    Env-decoupled: monkeypatches `_introspect.search` so the fast-path branch is hit
    deterministically regardless of how the live AST index scores any real need.
    Also monkeypatches `_discovery.discover` so we can assert the web-fallback was NOT
    invoked — guarding the actual code path, not just the tool_name.
    """
    ast_hit = {
        "name": "discover",
        "score": 1.0,
        "tool_class": "DiscoverTool",
        "doc": "...",
        "module": "hive.tools.builtins",
        "source": "ast",
    }
    monkeypatch.setattr(
        "hive.tools.builtins._introspect.search",
        lambda need, k=10, idx=None: [ast_hit],
    )

    mock_discover = MagicMock()
    monkeypatch.setattr("hive.tools.builtins._discovery.discover", mock_discover)

    d = DiscoverTool(enable_security_audit=False)
    out = asyncio.run(d.execute(need="anything"))

    # Fast path must NOT call the web discovery.
    mock_discover.assert_not_called()

    assert out.tool_name == "discover"
    payload = json.loads(out.content)
    assert payload["source"] == "ast"
    assert payload["need"] == "anything"
    assert payload["cached"] is False
    assert payload["candidates"] and payload["candidates"][0]["name"] == "discover"


def test_github_list_prs_returns_pr_summary(monkeypatch):
    """GitHubListPRs.execute() happy path (lines 648-660): builds the PR summary."""
    tool = GitHubListPRs(token="t", owner="o", repo="r")

    async def fake_get(self, path, params=None):
        assert path == "/repos/o/r/pulls"
        assert params == {"state": "open", "per_page": 10}
        return [
            {"number": 1, "title": "First", "state": "open", "draft": False,
             "user": {"login": "alice"}, "html_url": "https://gh/1"},
            {"number": 2, "title": "Second", "state": "open", "draft": True,
             "user": {"login": "bob"}, "html_url": "https://gh/2"},
        ]

    monkeypatch.setattr(GitHubListPRs, "_get", fake_get)
    out = asyncio.run(tool.execute(state="open", limit=10))
    assert out.tool_name == "github_list_prs"
    assert "2 PR(s)" in out.content
    assert "First" in out.content
    assert "draft" in out.content
    assert "alice" in out.content


def test_github_list_prs_returns_error_when_get_fails(monkeypatch):
    """GitHubListPRs surfaces a [github error: ...] message on _get failure."""
    tool = GitHubListPRs(token="t", owner="o", repo="r")

    async def fake_get(self, path, params=None):
        raise RuntimeError("403 forbidden")

    monkeypatch.setattr(GitHubListPRs, "_get", fake_get)
    out = asyncio.run(tool.execute())
    assert out.success is False
    assert "[github error:" in out.content
    assert "403 forbidden" in out.content


def test_github_list_commits_returns_commit_summary(monkeypatch):
    """GitHubListCommits.execute() happy path (lines 712-729): builds the commit summary."""
    tool = GitHubListCommits(token="t", owner="o", repo="r")

    async def fake_get(self, path, params=None):
        assert path == "/repos/o/r/commits"
        assert params == {"per_page": 5}  # no branch passed, limit=5 from caller
        return [
            {"sha": "deadbeef1234567890", "commit": {
                "message": "Initial commit\n\nbody ignored",
                "author": {"name": "alice", "date": "2026-06-25T00:00:00Z"},
            }},
        ]

    monkeypatch.setattr(GitHubListCommits, "_get", fake_get)
    out = asyncio.run(tool.execute(limit=5))
    assert out.tool_name == "github_list_commits"
    assert "deadbee" in out.content  # first 8 chars of sha
    assert "Initial commit" in out.content
    assert "alice" in out.content


def test_github_list_commits_passes_branch_param(monkeypatch):
    """When `branch` is supplied, it's forwarded as `sha` to the API."""
    tool = GitHubListCommits(token="t", owner="o", repo="r")

    async def fake_get(self, path, params=None):
        assert params == {"per_page": 5, "sha": "coverage/builtins-discovery-85"}
        return []

    monkeypatch.setattr(GitHubListCommits, "_get", fake_get)
    out = asyncio.run(tool.execute(branch="coverage/builtins-discovery-85", limit=5))
    assert "0 commits" not in out.content  # empty list but result formatted


def test_github_list_commits_returns_error_when_get_fails(monkeypatch):
    """GitHubListCommits surfaces [github error: ...] on failure."""
    tool = GitHubListCommits(token="t", owner="o", repo="r")

    async def fake_get(self, path, params=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(GitHubListCommits, "_get", fake_get)
    out = asyncio.run(tool.execute())
    assert out.success is False
    assert "[github error:" in out.content
    assert "network down" in out.content


def test_github_list_commits_no_token_returns_setup_hint():
    """GitHubListCommits without token/owner/repo returns the setup hint (line 714)."""
    tool = GitHubListCommits()  # no token, owner, repo → _available()=False
    out = asyncio.run(tool.execute())
    assert out.tool_name == "github_list_commits"
    assert out.success is False
    assert "HIVE_GITHUB_TOKEN" in out.content
    assert "OWNER" in out.content
    assert "REPO" in out.content
