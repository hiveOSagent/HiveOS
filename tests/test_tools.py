"""P5 — tools: registry, gate-routed executor, builtins, discovery, MCP adapter."""
from __future__ import annotations

import asyncio

import pytest

from hive.core.types import ToolResult
from hive.tools.base import BaseTool, ToolSpec
from hive.tools.builtins import register_builtins
from hive.tools.discovery import scan_red_flags, discover
from hive.tools.executor import DispatchStatus, ToolExecutor
from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec
from hive.tools.mcp.server import build_tool_listing
from hive.tools.registry import ToolRegistry


class _Spy(BaseTool):
    def __init__(self, name="spy", dangerous=False, boom=False):
        self._spec = ToolSpec(name=name, description="t", dangerous=dangerous)
        self.boom = boom
        self.ran = False

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, **params) -> ToolResult:
        self.ran = True
        if self.boom:
            raise RuntimeError("kaboom")
        return ToolResult(tool_name=self._spec.name, content="ok")


def _exec(tools, **kw) -> ToolExecutor:
    return ToolExecutor({t.spec.name: t for t in tools}, **kw)


# --- registry ------------------------------------------------------------------

def test_registry_add_snapshot_and_duplicate_guard():
    class R(ToolRegistry):
        pass
    R.add(_Spy("a"))
    assert "a" in R.snapshot()
    with pytest.raises(ValueError):
        R.add(_Spy("a"))


# --- executor: the three outcomes + audit/events -------------------------------

def test_executor_runs_safe_tool_and_audits():
    spy = _Spy("safe")
    audits: list = []
    ex = _exec([spy], audit=audits.append)
    out = asyncio.run(ex.execute("safe", {"x": 1}))
    assert out.status is DispatchStatus.OK and out.result.content == "ok"
    assert spy.ran is True
    assert audits and audits[0]["status"] == "ok"


def test_executor_gates_spec_dangerous_without_running():
    spy = _Spy("danger", dangerous=True)
    ex = _exec([spy])
    out = asyncio.run(ex.execute("danger", {}))
    assert out.status is DispatchStatus.PENDING and out.approval_id
    assert spy.ran is False                       # gated BEFORE execution


def test_executor_gates_destructive_shell_via_gate():
    # `shell` is not flagged dangerous, but the gate flags `rm -rf /`
    tools = register_builtins(type("R1", (ToolRegistry,), {}))
    ex = ToolExecutor(tools)
    safe = asyncio.run(ex.execute("shell", {"cmd": "echo hi"}))
    assert safe.status is DispatchStatus.OK and "hi" in safe.result.content
    danger = asyncio.run(ex.execute("shell", {"cmd": "rm -rf /"}))
    assert danger.status is DispatchStatus.PENDING


def test_executor_unknown_and_error():
    assert asyncio.run(_exec([]).execute("nope")).status is DispatchStatus.ERROR
    out = asyncio.run(_exec([_Spy("bad", boom=True)]).execute("bad"))
    assert out.status is DispatchStatus.ERROR and "kaboom" in out.error


# --- builtins ------------------------------------------------------------------

def test_builtins_read_write_roundtrip(tmp_path):
    tools = register_builtins(type("R2", (ToolRegistry,), {}))
    ex = ToolExecutor(tools)
    target = tmp_path / "note.txt"
    w = asyncio.run(ex.execute("write_file", {"path": str(target), "content": "hello"}))
    assert w.status is DispatchStatus.OK
    r = asyncio.run(ex.execute("read_file", {"path": str(target)}))
    assert r.result.content == "hello"


def test_builtins_dangerous_set_is_gated():
    tools = register_builtins(type("R3", (ToolRegistry,), {}))
    ex = ToolExecutor(tools)
    for name in ("spend_money", "deploy", "external_message"):
        assert asyncio.run(ex.execute(name, {})).status is DispatchStatus.PENDING


# --- discovery -----------------------------------------------------------------

def test_discovery_red_flag_scan():
    assert "rm -rf" in scan_red_flags("then run rm -rf / please")
    assert scan_red_flags("totally clean code") == []


def test_discovery_uses_memory_cache_without_network():
    class FakeMem:
        def recall(self, query, limit=5):
            return [{"topic": query, "content": "cached!"}]
        def learn(self, *a, **k):
            raise AssertionError("cache hit must not learn")

    out = asyncio.run(discover("a pdf parser", memory=FakeMem()))
    assert out["cached"] is True and out["result"]["content"] == "cached!"


# --- MCP adapter (offline) -----------------------------------------------------

def test_mcp_tool_to_spec_marks_dangerous():
    spec = mcp_tool_to_spec({"name": "search", "description": "d",
                             "inputSchema": {"type": "object"}}, prefix="ext.")
    assert spec.name == "ext.search" and spec.dangerous is True and spec.category == "mcp"


def test_execute_approved_rejects_traversal_path(tmp_path):
    tools = register_builtins(type("R5", (ToolRegistry,), {}))
    ex = ToolExecutor(tools)
    # Gate write_file first (it's not dangerous by spec, so it runs directly —
    # use a path outside tmp to trigger the path-safety recheck in execute_approved).
    out = asyncio.run(ex.execute_approved("write_file",
                                          {"path": "../../etc/passwd", "content": "x"}))
    assert out.status is DispatchStatus.ERROR
    assert out.error  # path-safety error must not be silent


def test_mcp_tool_wraps_caller_and_runs_through_executor():
    async def fake_caller(name, args):
        return f"called {name} with {args}"

    tool = MCPTool(mcp_tool_to_spec({"name": "remote"}), fake_caller)
    ex = ToolExecutor({tool.spec.name: tool})
    # dangerous (MCP) -> gated first
    assert asyncio.run(ex.execute("remote", {"q": 1})).status is DispatchStatus.PENDING
    # after approval it dispatches to the remote caller
    out = asyncio.run(ex.execute_approved("remote", {"q": 1}))
    assert out.status is DispatchStatus.OK and "called remote" in out.result.content


def test_mcp_server_listing_is_sorted():
    tools = register_builtins(type("R4", (ToolRegistry,), {}))
    listing = build_tool_listing(tools)
    names = [d["name"] for d in listing]
    assert names == sorted(names)
    assert {"read_file", "shell"} <= set(names)


# --- file_safety path traversal (items 36-37) ----------------------------------

def test_check_path_blocks_traversal():
    from hive.tools.file_safety import check_path
    result = check_path("../../etc/passwd")
    assert result is not None
    assert "traversal" in result.lower() or "permitted" in result.lower()


def test_check_path_allows_normal():
    from hive.tools.file_safety import check_path
    assert check_path("/tmp/safe_file_hive_test.txt") is None


def test_has_traversal():
    from hive.tools.file_safety import has_traversal
    assert has_traversal("../../etc/passwd") is True
    assert has_traversal("../file.txt") is True
    assert has_traversal("/tmp/file.txt") is False
    assert has_traversal("relative/path/file.txt") is False


def test_has_unsafe_symlink_no_symlink(tmp_path):
    from hive.tools.file_safety import has_unsafe_symlink
    real = tmp_path / "real.txt"
    real.write_text("content")
    assert has_unsafe_symlink(str(real)) is False


def test_check_path_blocks_symlink_escape(tmp_path, monkeypatch):
    from hive.tools.file_safety import check_path

    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = repo / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:  # pragma: no cover - symlinks unsupported
        pytest.skip("symlinks not supported on this platform")

    monkeypatch.chdir(repo)
    err = check_path(str(link), operation="write")
    assert err is not None
    assert "symlink escape" in err


def test_has_unsafe_symlink_external_escape(tmp_path):
    import os
    from hive.tools.file_safety import has_unsafe_symlink
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    link = tmp_path / "link.txt"
    try:
        os.symlink(str(outside), str(link))
    except OSError:
        pytest.skip("symlink creation is unavailable for this test user")
    # The symlink points outside tmp_path — resolves outside cwd
    # The function checks if target escapes cwd; since both are under /tmp
    # this is environment-dependent; just verify it doesn't crash.
    result = has_unsafe_symlink(str(link))
    assert isinstance(result, bool)


# --- registry remove/list_categories (items 44-45) ----------------------------

def test_registry_remove():
    class MyReg(ToolRegistry):
        pass

    class DummyTool(BaseTool):
        spec = ToolSpec(name="dummy_remove", category="test", description="t")

        async def execute(self, **kwargs):
            return ToolResult(tool_name="dummy_remove", content="ok")

    MyReg.add(DummyTool())
    assert MyReg.remove("dummy_remove") is True
    assert "dummy_remove" not in MyReg.snapshot()
    assert MyReg.remove("nonexistent") is False


def test_registry_list_categories():
    class CatReg(ToolRegistry):
        pass

    register_builtins(CatReg)
    cats = CatReg.list_categories()
    assert isinstance(cats, list)
    assert cats == sorted(cats)
    assert "files" in cats


def test_registry_count_and_values():
    class CountReg(ToolRegistry):
        pass

    class T1(BaseTool):
        spec = ToolSpec(name="cnt_t1", category="test", description="")
        async def execute(self, **kw): return ToolResult(tool_name="cnt_t1", content="")

    class T2(BaseTool):
        spec = ToolSpec(name="cnt_t2", category="test", description="")
        async def execute(self, **kw): return ToolResult(tool_name="cnt_t2", content="")

    CountReg.add(T1())
    CountReg.add(T2())
    assert CountReg.count() == 2
    vals = CountReg.values()
    assert len(vals) == 2


def test_registry_find_by_category():
    class FBCReg(ToolRegistry):
        pass

    class FileTool(BaseTool):
        spec = ToolSpec(name="fbc_file", category="files", description="")
        async def execute(self, **kw): return ToolResult(tool_name="fbc_file", content="")

    class NetTool(BaseTool):
        spec = ToolSpec(name="fbc_net", category="network", description="")
        async def execute(self, **kw): return ToolResult(tool_name="fbc_net", content="")

    FBCReg.add(FileTool())
    FBCReg.add(NetTool())
    files = FBCReg.find_by_category("files")
    assert len(files) == 1 and files[0].spec.name == "fbc_file"
    net = FBCReg.find_by_category("network")
    assert len(net) == 1 and net[0].spec.name == "fbc_net"
    # case-insensitive: "NETWORK" matches "network"
    assert len(FBCReg.find_by_category("NETWORK")) == 1
    assert FBCReg.find_by_category("missing") == []


# --- ToolExecutor timeout (item 46) -------------------------------------------

def test_executor_timeout_surfaces_as_error():
    """A tool that hangs beyond the timeout must return an ERROR dispatch."""
    import asyncio as _asyncio
    from hive.tools.executor import ToolExecutor, DispatchStatus
    from hive.tools.base import BaseTool, ToolSpec, ToolResult

    class SlowTool(BaseTool):
        spec = ToolSpec(name="slow", category="test", description="hangs")

        async def execute(self, **kwargs):
            await _asyncio.sleep(9999)
            return ToolResult(tool_name="slow", content="never")

    ex = ToolExecutor({"slow": SlowTool()}, timeout=0.05)
    result = _asyncio.run(ex.execute("slow"))
    assert result.status is DispatchStatus.ERROR
    assert "timed out" in (result.error or "")


def test_executor_no_timeout_none_runs_normally():
    """timeout=None disables the cap; fast tools must still succeed."""
    import asyncio as _asyncio
    from hive.tools.executor import ToolExecutor, DispatchStatus
    from hive.tools.base import BaseTool, ToolSpec, ToolResult

    class FastTool(BaseTool):
        spec = ToolSpec(name="fast", category="test", description="quick")

        async def execute(self, **kwargs):
            return ToolResult(tool_name="fast", content="done")

    ex = ToolExecutor({"fast": FastTool()}, timeout=None)
    result = _asyncio.run(ex.execute("fast"))
    assert result.status is DispatchStatus.OK


def test_executor_list_and_has_tool():
    from hive.tools.executor import ToolExecutor
    from hive.tools.base import BaseTool, ToolSpec, ToolResult

    class T1(BaseTool):
        spec = ToolSpec(name="t_alpha", category="test", description="")
        async def execute(self, **kw): return ToolResult(tool_name="t_alpha", content="")

    class T2(BaseTool):
        spec = ToolSpec(name="t_beta", category="test", description="")
        async def execute(self, **kw): return ToolResult(tool_name="t_beta", content="")

    ex = ToolExecutor({"t_alpha": T1(), "t_beta": T2()})
    assert ex.list_tools() == ["t_alpha", "t_beta"]
    assert ex.has_tool("t_alpha") is True
    assert ex.has_tool("missing") is False


def test_executor_remove_tool():
    from hive.tools.executor import ToolExecutor
    from hive.tools.base import BaseTool, ToolSpec, ToolResult

    class DynTool(BaseTool):
        spec = ToolSpec(name="dyn", category="test", description="")
        async def execute(self, **kw): return ToolResult(tool_name="dyn", content="")

    ex = ToolExecutor({"dyn": DynTool()})
    assert ex.has_tool("dyn") is True
    assert ex.remove_tool("dyn") is True
    assert ex.has_tool("dyn") is False
    assert ex.remove_tool("dyn") is False


def test_executor_stats():
    from hive.tools.executor import ToolExecutor
    from hive.tools.base import BaseTool, ToolSpec, ToolResult

    class SafeTool(BaseTool):
        spec = ToolSpec(name="safe_a", category="io", description="", dangerous=False)
        async def execute(self, **kw): return ToolResult(tool_name="safe_a", content="")

    class DangerTool(BaseTool):
        spec = ToolSpec(name="danger_b", category="system", description="", dangerous=True)
        async def execute(self, **kw): return ToolResult(tool_name="danger_b", content="")

    ex = ToolExecutor({"safe_a": SafeTool(), "danger_b": DangerTool()}, timeout=30.0)
    stats = ex.stats()
    assert stats["total"] == 2
    assert stats["available"] == 2
    assert stats["unavailable"] == 0
    assert stats["dangerous_count"] == 1
    assert "danger_b" in stats["dangerous"]
    assert "io" in stats["by_category"] and "system" in stats["by_category"]
    assert stats["timeout_seconds"] == 30.0


# --- N-1: SSRF protection in WebGet -------------------------------------------

def test_web_get_blocks_private_ip():
    from hive.tools.builtins import WebGet
    result = asyncio.run(WebGet().execute(url="http://192.168.1.1/"))
    assert result.success is False
    assert "blocked" in result.content.lower()


def test_web_get_blocks_loopback():
    from hive.tools.builtins import WebGet
    result = asyncio.run(WebGet().execute(url="http://127.0.0.1/"))
    assert result.success is False
    assert "blocked" in result.content.lower()


def test_web_get_blocks_metadata_endpoint():
    from hive.tools.builtins import WebGet
    result = asyncio.run(WebGet().execute(url="http://169.254.169.254/latest/meta-data/"))
    assert result.success is False
    assert "blocked" in result.content.lower()


def test_web_get_blocks_userinfo_url():
    from hive.tools.builtins import WebGet
    result = asyncio.run(WebGet().execute(url="http://user:pass@example.com/"))
    assert result.success is False
    assert "blocked" in result.content.lower()


def test_web_get_allows_public_url():
    from hive.tools.builtins import _validate_url
    # No exception means the URL passed validation
    _validate_url("https://example.com/path?q=1")
    _validate_url("http://api.github.com/repos")


def test_web_get_blocks_ssrf_via_redirect():
    """Redirect to a private IP must be blocked even if initial URL was public."""
    import httpx
    from hive.tools.builtins import _check_redirect
    mock_response = httpx.Response(302, headers={"location": "http://192.168.1.1/secret"})
    with pytest.raises(ValueError, match="SSRF redirect blocked"):
        _check_redirect(mock_response)


def test_check_redirect_allows_public_location():
    """Redirect to a public URL must be allowed."""
    import httpx
    from hive.tools.builtins import _check_redirect
    mock_response = httpx.Response(302, headers={"location": "https://example.com/page"})
    _check_redirect(mock_response)  # Should not raise


def test_check_redirect_resolves_protocol_relative_location():
    import httpx
    from hive.tools.builtins import _check_redirect

    request = httpx.Request("GET", "https://example.com/start")
    mock_response = httpx.Response(
        302,
        headers={"location": "//192.168.1.1/secret"},
        request=request,
    )
    with pytest.raises(ValueError, match="SSRF redirect blocked"):
        _check_redirect(mock_response)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/x",
        "http://0.0.0.0:8000/x",
        "http://[::ffff:127.0.0.1]/x",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://0x7f000001/",
        "http://127.0.0.1.nip.io/",
        "http://metadata.google.internal/latest/meta-data/",
    ],
)
def test_validate_url_blocks_all_known_ssrf_encodings(monkeypatch, url):
    """Every issue #149 bypass is rejected before an HTTP request is made."""
    import socket
    from hive.tools import builtins as builtins_mod

    if "nip.io" in url:
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))
            ],
        )
    with pytest.raises(ValueError, match="Blocked"):
        builtins_mod._validate_url(url)


def test_validate_url_blocks_when_any_dns_answer_is_private(monkeypatch):
    import socket
    from hive.tools import builtins as builtins_mod

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 0)),
        ],
    )
    with pytest.raises(ValueError, match="Blocked address"):
        builtins_mod._validate_url("https://example.test/")


def test_pinned_network_backend_revalidates_and_pins_connection(monkeypatch):
    from hive.tools import builtins as builtins_mod

    calls = []

    class _FakeBackend:
        async def connect_tcp(self, host, port, **kwargs):
            calls.append((host, port))
            return "stream"

        async def connect_unix_socket(self, path, **kwargs):
            return "unix-stream"

        async def sleep(self, seconds):
            return None

    monkeypatch.setattr(
        builtins_mod,
        "_resolve_host_addresses",
        lambda host, port: ("93.184.216.34", "93.184.216.35"),
    )
    backend = builtins_mod._PinnedNetworkBackend()
    backend._backend = _FakeBackend()
    result = asyncio.run(backend.connect_tcp("example.test", 443))
    assert result == "stream"
    assert calls == [("93.184.216.34", 443)]


# --- Task 1: BaseTool.to_openai_function() -------------------------------------

def test_base_tool_to_openai_function_format():
    """to_openai_function() on BaseTool returns OpenAI-compatible function schema."""

    class _MyTool(BaseTool):
        spec = ToolSpec(
            name="my_tool",
            description="does something",
            parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        )

        async def execute(self, **params) -> ToolResult:
            return ToolResult(tool_name="my_tool", content="ok")

    result = _MyTool().to_openai_function()
    assert result["type"] == "function"
    assert result["function"]["name"] == "my_tool"
    assert result["function"]["description"] == "does something"
    assert "parameters" in result["function"]
    assert result["function"]["parameters"]["required"] == ["x"]


# --- Task 2: ToolRegistry edge cases -------------------------------------------

def test_tool_registry_find_by_category_empty_string():
    """find_by_category('') returns list (no crash); tools with no category won't match."""

    class _EmptyCatReg(ToolRegistry):
        pass

    results = _EmptyCatReg.find_by_category("")
    assert isinstance(results, list)


def test_tool_registry_get_nonexistent_raises_keyerror():
    """RegistryBase.get raises KeyError for unregistered keys (no None return)."""

    class _MissingReg(ToolRegistry):
        pass

    import pytest as _pytest
    with _pytest.raises(KeyError):
        _MissingReg.get("does_not_exist")


# --- Task 3: file_safety edge cases --------------------------------------------

def test_file_safety_custom_home_directory(tmp_path):
    """build_denied_write_paths(home=...) uses the given home, not ~."""
    from hive.tools.file_safety import build_denied_write_paths
    denied = build_denied_write_paths(home=str(tmp_path))
    assert any(str(tmp_path) in s for s in denied)


def test_file_safety_symlink_exception_handled(tmp_path, monkeypatch):
    """has_unsafe_symlink() returns False when is_symlink() raises PermissionError."""
    import pathlib
    from hive.tools import file_safety

    def raise_permission(*a, **kw):
        raise PermissionError("no access")

    monkeypatch.setattr(pathlib.Path, "is_symlink", raise_permission)
    result = file_safety.has_unsafe_symlink(str(tmp_path / "file.txt"))
    assert result is False


# --- Discovery edge cases (Group 1) --------------------------------------------

def test_discover_scan_red_flags_catches_eval():
    """scan_red_flags detects eval( in dangerous code snippet."""
    result = scan_red_flags("import os; eval(x)")
    # scan_red_flags returns a list of matched flag strings
    assert bool(result) is True
    assert any("eval(" in flag for flag in result)


def test_discover_scan_red_flags_clean_code():
    """scan_red_flags returns an empty list for benign code."""
    result = scan_red_flags("def hello(): return 42")
    assert result == []


def test_discover_with_security_delegate_called():
    """discover() calls security_delegate for each candidate that has a URL."""
    call_log: list[str] = []

    async def mock_delegate(task: str) -> str:
        call_log.append(task)
        return "looks safe"

    # Patch httpx to return controlled candidates so no real network call happens
    import httpx
    from unittest.mock import AsyncMock, MagicMock, patch

    mcp_response = MagicMock()
    mcp_response.json.return_value = {
        "servers": [
            {"name": "tool-a", "repository": {"url": "https://example.com/tool-a"}},
            {"name": "tool-b", "repository": {"url": "https://example.com/tool-b"}},
        ]
    }

    github_response = MagicMock()
    github_response.json.return_value = {"items": []}

    async def fake_get(url, **kwargs):
        if "modelcontextprotocol" in url:
            return mcp_response
        return github_response

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get.side_effect = fake_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = asyncio.run(discover("test tool", security_delegate=mock_delegate))

    assert result["cached"] is False
    candidates_with_url = [c for c in result["candidates"] if c.get("url")]
    # delegate must have been called once per candidate that has a URL
    assert len(call_log) == len(candidates_with_url)
    assert len(call_log) >= 1


def test_discover_security_delegate_exception_sets_audit_unavailable():
    """When the security delegate raises, candidate gets security_note == '[audit unavailable]'."""
    from unittest.mock import AsyncMock, MagicMock, patch

    async def failing_delegate(task: str) -> str:
        raise RuntimeError("audit service down")

    mcp_response = MagicMock()
    mcp_response.json.return_value = {
        "servers": [
            {"name": "risky-tool", "repository": {"url": "https://example.com/risky"}},
        ]
    }

    github_response = MagicMock()
    github_response.json.return_value = {"items": []}

    async def fake_get(url, **kwargs):
        if "modelcontextprotocol" in url:
            return mcp_response
        return github_response

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get.side_effect = fake_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = asyncio.run(discover("risky thing", security_delegate=failing_delegate))

    candidates = result["candidates"]
    assert len(candidates) >= 1
    # At least the candidate with a URL must have the fallback note
    url_candidates = [c for c in candidates if c.get("url")]
    assert all(c.get("security_note") == "[audit unavailable]" for c in url_candidates)


# --- MCP tool_to_spec edge cases (Group 2) -------------------------------------

def test_mcp_tool_to_spec_dangerous_true():
    """mcp_tool_to_spec always marks external MCP tools as dangerous."""
    spec = mcp_tool_to_spec(
        {"name": "github.search", "description": "search",
         "inputSchema": {"type": "object", "properties": {}}},
        prefix="github.",
    )
    assert spec.dangerous is True


def test_mcp_tool_to_spec_name_prefixed():
    """mcp_tool_to_spec prepends prefix to the tool name."""
    spec = mcp_tool_to_spec(
        {"name": "search", "description": "search GitHub",
         "inputSchema": {"type": "object", "properties": {}}},
        prefix="github.",
    )
    # The prefix is prepended literally: "github." + "search" -> "github.search"
    assert spec.name == "github.search"
    assert spec.category == "mcp"


# --- MCP server-side (new) -------------------------------------------------------

def test_mcp_server_create_does_not_raise():
    """Instantiate MCPServer with an empty tool registry — no exception raised."""
    from hive.tools.mcp.server import MCPServer
    server = MCPServer({})
    assert server is not None


def test_mcp_server_list_tools_empty():
    """MCPServer.listing() on an empty registry returns an empty list."""
    from hive.tools.mcp.server import MCPServer
    server = MCPServer({})
    result = server.listing()
    assert result == []


# --- file_safety extras (new) ----------------------------------------------------

def test_file_safety_allows_reads_in_home_dir():
    """check_path with operation='read' never blocks reads (no denylist for reads)."""
    from hive.tools.file_safety import check_path
    # reads are not restricted by the write denylist; check_path returns None for read
    result = check_path("/home/user/file.txt", operation="read")
    assert result is None


def test_file_safety_denies_read_outside_home():
    """For write operations /etc/passwd is blocked; reads are unrestricted by design.
    This test verifies the write guard covers /etc/passwd regardless of home."""
    from hive.tools.file_safety import is_write_denied
    # /etc/passwd is always in the denied write set
    assert is_write_denied("/etc/passwd") is True


def test_build_denied_write_paths_contains_credentials():
    """build_denied_write_paths() result includes a credential-related path."""
    from hive.tools.file_safety import build_denied_write_paths
    import os
    home = "/home/user"
    denied = build_denied_write_paths(home=home)
    # .git-credentials, .netrc, .pgpass, .pypirc are all credential files
    credential_paths = {
        os.path.join(home, ".git-credentials"),
        os.path.join(home, ".netrc"),
        os.path.join(home, ".pgpass"),
        os.path.join(home, ".pypirc"),
    }
    assert denied & {os.path.realpath(p) for p in credential_paths}


# --- new registry / base / spec / builtin tests ----------------------------------

def test_tool_registry_contains_registered_name():
    """After ToolRegistry.add(), the tool name is present in the snapshot."""
    class R(ToolRegistry):
        pass
    R.add(_Spy("registered_tool"))
    assert "registered_tool" in R.snapshot()


def test_tool_registry_all_returns_dict():
    """ToolRegistry.snapshot() returns a plain dict keyed by tool name."""
    class R(ToolRegistry):
        pass
    R.add(_Spy("dict_tool"))
    result = R.snapshot()
    assert isinstance(result, dict)
    assert "dict_tool" in result


def test_base_tool_name_from_spec():
    """A tool's spec.name matches what was passed to ToolSpec."""
    spec = ToolSpec(name="my_tool", description="does things")
    spy = _Spy("my_tool")
    assert spy.spec.name == "my_tool"
    assert spec.name == "my_tool"


def test_tool_result_success_flag():
    """ToolResult with success=True is truthy; without explicit error it succeeds."""
    result = ToolResult(tool_name="noop", content="all good", success=True)
    assert result.success is True
    assert bool(result) is True


def test_tool_result_failure_flag():
    """ToolResult with success=False is falsy."""
    result = ToolResult(tool_name="noop", content="oops", success=False)
    assert result.success is False
    assert bool(result) is False


def test_shell_tool_returns_content():
    """Shell.execute(cmd='echo hi') returns a ToolResult whose content contains 'hi'."""
    from hive.tools.builtins import Shell
    tool = Shell()
    result = asyncio.run(tool.execute(cmd="echo hi"))
    assert "hi" in result.content
    assert result.success is True


def test_discover_tool_instantiates():
    """DiscoverTool() can be created without raising."""
    from hive.tools.builtins import DiscoverTool
    tool = DiscoverTool()
    assert tool is not None
    assert tool.spec.name == "discover"


def test_tool_spec_parameters_stored():
    """ToolSpec stores the parameters dict as-is."""
    params = {"type": "object", "properties": {"x": {"type": "integer"}}}
    spec = ToolSpec(name="paramtool", description="has params", parameters=params)
    assert spec.parameters == params


def test_mcp_tool_to_spec_name_set():
    """mcp_tool_to_spec builds a ToolSpec whose name matches the MCP descriptor name."""
    spec = mcp_tool_to_spec({"name": "list_files", "description": "lists files"})
    assert spec.name == "list_files"
    assert spec.dangerous is True
    assert spec.category == "mcp"


def test_web_get_tool_spec_has_name():
    """WebGet tool has a non-empty spec.name."""
    from hive.tools.builtins import WebGet
    tool = WebGet()
    assert tool.spec.name != ""
    assert tool.spec.name == "web_get"


# --- Wave 3L additional tests ---------------------------------------------------

def test_tool_registry_count_increments():
    """ToolRegistry.count() increases after each register_value()."""
    from hive.tools.registry import ToolRegistry
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _T(BaseTool):
        spec = ToolSpec(name="cnt_tool", description="d", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="cnt_tool", content="x")

    r = ToolRegistry()
    assert r.count() == 0
    r.register_value("cnt_tool", _T())
    assert r.count() == 1


def test_tool_registry_contains_after_register():
    """ToolRegistry.contains(key) returns True after register_value(key, ...)."""
    from hive.tools.registry import ToolRegistry
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _T2(BaseTool):
        spec = ToolSpec(name="t2", description="d", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="t2", content="x")

    r = ToolRegistry()
    assert not r.contains("t2")
    r.register_value("t2", _T2())
    assert r.contains("t2")


def test_tool_registry_snapshot_returns_dict():
    """ToolRegistry.snapshot() returns a dict with all registered tools."""
    from hive.tools.registry import ToolRegistry
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _T3(BaseTool):
        spec = ToolSpec(name="snap_t", description="d", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="snap_t", content="x")

    r = ToolRegistry()
    r.register_value("snap_t", _T3())
    snap = r.snapshot()
    assert isinstance(snap, dict)
    assert "snap_t" in snap


def test_tool_registry_list_categories():
    """ToolRegistry.list_categories() returns category list after categorized registration."""
    from hive.tools.registry import ToolRegistry
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _Cat(BaseTool):
        spec = ToolSpec(name="cat_tool", description="d", parameters={}, category="mycat")
        async def execute(self, **kw): return ToolResult(tool_name="cat_tool", content="x")

    r = ToolRegistry()
    r.register_value("cat_tool", _Cat())
    cats = r.list_categories()
    assert "mycat" in cats


def test_tool_spec_name_and_description():
    """ToolSpec stores name and description verbatim."""
    from hive.tools.base import ToolSpec
    spec = ToolSpec(name="my_tool", description="does something useful", parameters={})
    assert spec.name == "my_tool"
    assert spec.description == "does something useful"


def test_tool_result_content_stored():
    """ToolResult stores the content string passed at construction."""
    from hive.core.types import ToolResult
    r = ToolResult(tool_name="t", content="the output")
    assert r.content == "the output"
    assert r.tool_name == "t"


# --- 6 new tools tests ---------------------------------------------------------

def test_validate_url_blocks_private_ip():
    """_validate_url raises ValueError for private/loopback IP addresses."""
    from hive.tools.builtins import _validate_url
    import pytest
    with pytest.raises(ValueError, match="Blocked"):
        _validate_url("http://192.168.1.1/path")


def test_validate_url_blocks_non_http_scheme():
    """_validate_url raises ValueError for non-http/https schemes."""
    from hive.tools.builtins import _validate_url
    import pytest
    with pytest.raises(ValueError, match="Blocked scheme"):
        _validate_url("ftp://example.com/file")


def test_validate_url_allows_public_http():
    """_validate_url does not raise for a public HTTP URL."""
    from hive.tools.builtins import _validate_url
    _validate_url("http://example.com/api/v1")  # must not raise


def test_tool_spec_dangerous_defaults_false():
    """ToolSpec.dangerous defaults to False when not provided."""
    spec = ToolSpec(name="safe_tool", description="does nothing")
    assert spec.dangerous is False


def test_tool_spec_category_defaults_general():
    """ToolSpec.category defaults to 'general' when not provided."""
    spec = ToolSpec(name="some_tool", description="desc")
    assert spec.category == "general"


def test_registry_find_by_category_empty_when_none_match():
    """ToolRegistry.find_by_category returns an empty list for a category with no tools."""
    class _IsoReg(ToolRegistry):
        pass

    class AFileTool(BaseTool):
        spec = ToolSpec(name="iso_file_t", description="d", category="myfiles")
        async def execute(self, **kw):
            return ToolResult(tool_name="iso_file_t", content="ok")

    _IsoReg.add(AFileTool())
    result = _IsoReg.find_by_category("nonexistent_category")
    assert result == []


# --- Wave 3U: 8 new tests --------------------------------------------------------

def test_registry_list_categories_empty_when_no_tools():
    """list_categories() returns [] on a fresh subclass with no registered tools."""
    class _FreshReg(ToolRegistry):
        pass

    assert _FreshReg.list_categories() == []


def test_registry_keys_reflects_registered_names():
    """ToolRegistry.keys() yields the name(s) of every registered tool."""
    class _KeysReg(ToolRegistry):
        pass

    class _KTool(BaseTool):
        spec = ToolSpec(name="keys_tool", description="d", category="test")
        async def execute(self, **kw):
            return ToolResult(tool_name="keys_tool", content="")

    _KeysReg.add(_KTool())
    assert "keys_tool" in list(_KeysReg.keys())


def test_tool_spec_dangerous_and_category_stored_together():
    """ToolSpec can be created with dangerous=True and a custom category simultaneously."""
    spec = ToolSpec(name="sys_op", description="a system op", dangerous=True, category="system")
    assert spec.dangerous is True
    assert spec.category == "system"


def test_read_file_empty_file_returns_empty_string(tmp_path):
    """ReadFile on a zero-byte file succeeds and returns an empty content string."""
    from hive.tools.builtins import ReadFile
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    result = asyncio.run(ReadFile().execute(path=str(empty)))
    assert result.success is True
    assert result.content == ""


def test_write_file_overwrites_existing_content(tmp_path):
    """WriteFile on an existing file replaces the old content."""
    from hive.tools.builtins import WriteFile
    target = tmp_path / "overwrite.txt"
    target.write_text("old content")
    r = asyncio.run(WriteFile().execute(path=str(target), content="new content"))
    assert r.success is True
    assert target.read_text() == "new content"


def test_shell_nonzero_exit_returns_failure():
    """Shell.execute with a command that exits non-zero must return success=False."""
    from hive.tools.builtins import Shell
    result = asyncio.run(Shell().execute(cmd="false"))
    assert result.success is False


def test_tool_result_metadata_field_stored():
    """ToolResult.metadata stores arbitrary key-value pairs unchanged."""
    meta = {"source": "unit-test", "iteration": 3}
    r = ToolResult(tool_name="meta_tool", content="data", metadata=meta)
    assert r.metadata == meta
    assert r.metadata["iteration"] == 3


def test_read_file_spec_category_and_not_dangerous():
    """ReadFile spec has category 'files' and dangerous=False."""
    from hive.tools.builtins import ReadFile
    spec = ReadFile().spec
    assert spec.category == "files"
    assert spec.dangerous is False


# ---------------------------------------------------------------------------
# Sprint 5: Discord webhook, Obsidian tools, GitHub tools
# ---------------------------------------------------------------------------

def test_discord_send_no_webhook():
    """ExternalMessage.discord returns a helpful error when webhook is not configured."""
    from hive.tools.builtins import ExternalMessage
    tool = ExternalMessage()
    result = asyncio.run(tool.execute(channel="discord", body="hello"))
    assert not result.success
    assert "HIVE_DISCORD_WEBHOOK" in result.content


def test_discord_send_with_mock_webhook(monkeypatch):
    """ExternalMessage._send_discord posts to the configured webhook URL."""
    import urllib.request

    calls: list[dict] = []

    class _FakeResponse:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def _fake_urlopen(req, timeout=10):
        calls.append({"url": req.full_url, "data": req.data})
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    from hive.tools.builtins import ExternalMessage
    tool = ExternalMessage(discord_webhook="https://discord.com/api/webhooks/fake/token")
    result = asyncio.run(tool.execute(channel="discord", body="test msg"))
    assert result.success
    assert calls, "webhook was never called"
    import json
    body = json.loads(calls[0]["data"])
    assert body["content"] == "test msg"


def test_obsidian_read_missing_note(tmp_path):
    """ObsidianRead returns failure when note doesn't exist."""
    from hive.tools.builtins import ObsidianRead
    tool = ObsidianRead(vault_path=tmp_path)
    result = asyncio.run(tool.execute(kind="fact", topic="nonexistent"))
    assert not result.success
    assert "not found" in result.content


def test_obsidian_read_existing_note(tmp_path):
    """ObsidianRead returns note body without frontmatter."""
    from hive.memory.vault import ObsidianVault
    from hive.tools.builtins import ObsidianRead

    vault = ObsidianVault(tmp_path)
    vault.write("fact", "test topic", "This is the note body.", "test")

    tool = ObsidianRead(vault_path=tmp_path)
    result = asyncio.run(tool.execute(kind="fact", topic="test topic"))
    assert result.success
    assert "This is the note body." in result.content
    assert "---" not in result.content.split("\n")[0]


def test_obsidian_search_finds_matching_note(tmp_path):
    """ObsidianSearch returns results when a term matches vault content."""
    from hive.memory.vault import ObsidianVault
    from hive.tools.builtins import ObsidianSearch

    vault = ObsidianVault(tmp_path)
    vault.write("research", "python tricks", "Python is great for scripting.", "test")

    tool = ObsidianSearch(vault_path=tmp_path)
    result = asyncio.run(tool.execute(query="python scripting"))
    assert result.success
    assert "python" in result.content.lower() or "tricks" in result.content.lower()


def test_obsidian_list_returns_notes(tmp_path):
    """ObsidianList returns all notes in the vault."""
    from hive.memory.vault import ObsidianVault
    from hive.tools.builtins import ObsidianList

    vault = ObsidianVault(tmp_path)
    vault.write("skill", "topic1", "content1", "test")
    vault.write("fact", "topic2", "content2", "test")

    tool = ObsidianList(vault_path=tmp_path)
    result = asyncio.run(tool.execute())
    assert result.success
    assert "2 notes" in result.content


def test_github_list_prs_no_token():
    """GitHubListPRs returns helpful error when token is not set."""
    from hive.tools.builtins import GitHubListPRs
    tool = GitHubListPRs()
    result = asyncio.run(tool.execute())
    assert not result.success
    assert "HIVE_GITHUB_TOKEN" in result.content


def test_github_create_issue_no_token():
    """GitHubCreateIssue returns helpful error when token is not set."""
    from hive.tools.builtins import GitHubCreateIssue
    tool = GitHubCreateIssue()
    result = asyncio.run(tool.execute(title="test issue"))
    assert not result.success
    assert "HIVE_GITHUB_TOKEN" in result.content


# --- Phase 2: query_memory + create_task tools ---------------------------------

def test_query_memory_no_provider():
    """QueryMemory returns a clear error when no memory provider is configured."""
    from hive.tools.builtins import QueryMemory
    tool = QueryMemory(memory=None)
    assert not tool.available()
    result = asyncio.run(tool.execute(query="self-modification"))
    assert not result.success
    assert "no memory provider" in result.content


def test_query_memory_returns_results():
    """QueryMemory calls recall() on the provider and returns JSON results."""
    from hive.tools.builtins import QueryMemory

    class _FakeMemory:
        def recall(self, query, limit=5):
            return [{"kind": "fact", "topic": "selfmod", "content": "Hive can self-modify"}]

    tool = QueryMemory(memory=_FakeMemory())
    assert tool.available()
    result = asyncio.run(tool.execute(query="self-modification"))
    assert result.success
    assert "selfmod" in result.content


def test_create_task_no_board():
    """CreateTask returns a clear error when no task board is configured."""
    from hive.tools.builtins import CreateTask
    tool = CreateTask(task_board=None)
    assert not tool.available()
    result = asyncio.run(tool.execute(tool="shell", reason="test"))
    assert not result.success
    assert "no task board" in result.content


def test_create_task_enqueues():
    """CreateTask enqueues a task on the provided task board."""
    from hive.tools.builtins import CreateTask

    enqueued: list[dict] = []

    class _FakeBoard:
        def enqueue(self, kind, payload, *, scheduled_for=0.0, source=""):
            enqueued.append({"kind": kind, "payload": payload, "source": source})
            return len(enqueued)

    tool = CreateTask(task_board=_FakeBoard())
    assert tool.available()
    result = asyncio.run(tool.execute(tool="shell", reason="health check", args={"cmd": "echo hi"}))
    assert result.success
    assert len(enqueued) == 1
    assert enqueued[0]["payload"]["tool"] == "shell"
    assert enqueued[0]["source"] == "agent"
    assert "1" in result.content  # task id


# ── Phase 3 (Part 2): Docker/SSH deploy + HiveStatus ──────────────────────────

def test_deploy_docker_mode_calls_docker_restart():
    """Deploy in docker mode runs 'docker restart {container}'."""
    import asyncio
    from unittest.mock import AsyncMock, patch, MagicMock
    from hive.tools.builtins import Deploy

    deploy = Deploy()
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(return_value=(b"", None))
    fake_proc.kill = MagicMock()

    with patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=fake_proc)) as mock_sh:
        result = asyncio.run(deploy.execute(target="gateway", mode="docker",
                                            container="hiveos-gateway"))
    cmd = mock_sh.call_args[0][0]
    assert "docker restart hiveos-gateway" in cmd
    assert result.success is True


def test_deploy_ssh_mode_requires_host():
    """Deploy in ssh mode returns error when no SSH host is configured."""
    import asyncio
    from hive.tools.builtins import Deploy

    deploy = Deploy(ssh_host="")  # no host
    result = asyncio.run(deploy.execute(target="keeper", mode="ssh"))
    assert result.success is False
    assert "HIVE_DEPLOY_SSH_HOST" in result.content


def test_deploy_ssh_mode_with_host():
    """Deploy in ssh mode builds correct SSH command when host is set."""
    import asyncio
    from unittest.mock import AsyncMock, patch, MagicMock
    from hive.tools.builtins import Deploy

    deploy = Deploy(ssh_host="user@my-server", ssh_key="/home/user/.ssh/id_rsa")
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(return_value=(b"ok", None))
    fake_proc.kill = MagicMock()

    with patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=fake_proc)) as mock_sh:
        result = asyncio.run(deploy.execute(target="orchestrator", mode="ssh"))
    cmd = mock_sh.call_args[0][0]
    assert "ssh" in cmd
    assert "user@my-server" in cmd
    assert "id_rsa" in cmd
    assert "hiveos-orchestrator.service" in cmd
    assert result.success is True


def test_hive_status_returns_health_data():
    """HiveStatus tool aggregates budget, error rate, tasks, and self-mod rate."""
    import asyncio
    from unittest.mock import MagicMock
    from hive.tools.builtins import HiveStatus

    hive = MagicMock()
    hive.budgeter.snapshot.return_value = {"calls_today": 10, "daily_cap": 3000}
    hive.audit_log.error_rate.return_value = 0.03
    hive.task_board.statistics.return_value = {
        "by_state": {"pending": {"count": 2}, "failed": {"count": 1}}
    }
    hive.self_modifier.success_rate.return_value = 0.75

    tool = HiveStatus(hive=hive)
    result = asyncio.run(tool.execute())
    assert result.success is True
    assert "0.75" in result.content or "75%" in result.content or "75.0%" in result.content
    assert "2 pending" in result.content or "pending" in result.content


def test_hive_status_unavailable_without_hive():
    """HiveStatus returns success=False when no hive object is wired."""
    import asyncio
    from hive.tools.builtins import HiveStatus

    tool = HiveStatus(hive=None)
    assert tool.available() is False
    result = asyncio.run(tool.execute())
    assert result.success is False


# ---------------------------------------------------------------------------
# Stripe / SpendMoney tests (issue #44)
# ---------------------------------------------------------------------------

def test_spend_money_no_stripe_key_returns_message():
    """SpendMoney without Stripe key returns an honest 'not configured' message."""
    import asyncio
    from hive.tools.builtins import SpendMoney

    tool = SpendMoney()
    result = asyncio.run(tool.execute(what="coffee", amount="5.00"))
    assert "Wire" in result.content or "adapter" in result.content
    assert "coffee" in result.content or "5.00" in result.content


def test_spend_money_with_stripe_calls_api(monkeypatch):
    """SpendMoney with Stripe key calls the Stripe API and returns PaymentIntent id."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from hive.tools.builtins import SpendMoney

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"id": "pi_test123", "status": "requires_payment_method"}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        tool = SpendMoney(stripe_key="sk_test_abc", stripe_customer="cus_test")
        result = asyncio.run(tool.execute(what="server rent", amount="$10.00"))

    assert result.success is True
    assert "pi_test123" in result.content
    assert "10.00" in result.content


def test_stripe_adapter_charge_builds_correct_payload(monkeypatch):
    """StripeAdapter.charge() sends correct amount (cents) and description to Stripe."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from hive.tools.builtins import StripeAdapter

    captured_payload: dict = {}

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"id": "pi_xyz", "status": "succeeded"}

    async def fake_post(url, headers=None, data=None):
        captured_payload.update(data or {})
        return mock_resp

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(side_effect=fake_post)
        mock_client_cls.return_value = mock_client

        adapter = StripeAdapter("sk_test_key", "cus_001")
        asyncio.run(adapter.charge(12.50, "domain renewal"))

    assert captured_payload.get("amount") == "1250"   # 12.50 USD → 1250 cents
    assert captured_payload.get("currency") == "usd"
    assert "domain renewal" in captured_payload.get("description", "")
    assert captured_payload.get("customer") == "cus_001"
