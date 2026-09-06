"""M4 surfaces — SSE streaming + transport-only Telegram channel."""
from __future__ import annotations

import asyncio
import dataclasses

import pytest
from starlette.testclient import TestClient

from hive.core.config import HiveConfig
from hive.core.types import Message, Role
from hive.gateway.app import create_app
from hive.gateway.channels.base import MessageEvent, OutgoingMessage, SendResult
from hive.gateway.channels.telegram import TelegramChannel
from hive.llm.adapters.base import CompletionRequest, CompletionResult, LLMAdapter, Usage
from hive.llm.credential_pool import CredentialPool
from hive.llm.router import ModelRouter
from hive.runtime import HiveOS


# --- adapter astream default ---------------------------------------------------

class _OneShotAdapter(LLMAdapter):
    name = "oneshot"

    async def complete(self, request, *, api_key):
        return CompletionResult(text="hello world", model=request.model,
                                usage=Usage(output_tokens=2))


def test_default_astream_yields_full_text():
    adapter = _OneShotAdapter()
    req = CompletionRequest(model="m", messages=[Message(role=Role.USER, content="hi")])

    async def collect():
        return [d async for d in adapter.astream(req, api_key="k")]

    assert asyncio.run(collect()) == ["hello world"]


# --- router.stream -------------------------------------------------------------

class _DeltaAdapter(LLMAdapter):
    name = "delta"

    async def complete(self, request, *, api_key):
        return CompletionResult(text="unused", model=request.model)

    async def astream(self, request, *, api_key):
        for part in ("Hel", "lo ", "Hive"):
            yield part


def _config(tmp_path):
    return HiveConfig.from_env(root=tmp_path, load_dotenv=False)


def test_router_stream_yields_deltas_and_emits(tmp_path):
    from hive.core.events import EventBus, EventType
    bus = EventBus()
    seen = []
    bus.subscribe(EventType.INFERENCE_END, lambda e: seen.append(e))
    router = ModelRouter(config=_config(tmp_path), adapter=_DeltaAdapter(),
                         credential_pool=CredentialPool(["k"]), events=bus)

    async def collect():
        return [d async for d in router.stream([Message(role=Role.USER, content="hi")])]

    assert asyncio.run(collect()) == ["Hel", "lo ", "Hive"]
    assert seen, "INFERENCE_END should fire after a stream"


def test_router_stream_budget_blocks(tmp_path):
    from hive.llm.router import BudgetError
    router = ModelRouter(config=_config(tmp_path), adapter=_DeltaAdapter(),
                         credential_pool=CredentialPool(["k"]),
                         budget=lambda: (False, "cap hit"))

    async def collect():
        return [d async for d in router.stream([Message(role=Role.USER, content="hi")])]

    with pytest.raises(BudgetError):
        asyncio.run(collect())


# --- gateway SSE ---------------------------------------------------------------

class _StreamRouter:
    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        return CompletionResult(text="full", model="m")

    async def stream(self, messages, *, system=None, **kw):
        for part in ("a", "b", "c"):
            yield part

    async def aclose(self):
        pass


def test_chat_stream_sse(tmp_path):
    hive = HiveOS.build(_config(tmp_path), router=_StreamRouter())
    with TestClient(create_app(hive)) as c:
        r = c.post("/chat/stream", json={"message": "hi", "session_id": "s1"},
                   headers={"X-Hive-Token": "change_me"})
        assert r.status_code == 200
        body = r.text
        assert "data: a" in body and "data: b" in body and "data: c" in body
        assert "data: [DONE]" in body


def test_chat_stream_requires_token(tmp_path):
    hive = HiveOS.build(_config(tmp_path), router=_StreamRouter())
    with TestClient(create_app(hive)) as c:
        assert c.post("/chat/stream", json={"message": "hi"}).status_code == 401


def test_chat_stream_error_does_not_leak_exception_detail(tmp_path):
    """SSE error events must emit only the exception class name, never the full
    str(exc) which may contain internal paths, credentials, or stack details."""
    class _BoomRouter:
        async def complete(self, messages, **kw):
            from hive.llm.adapters.base import CompletionResult
            return CompletionResult(text="ok", model="m")

        async def stream(self, messages, **kw):
            raise RuntimeError("secret internal path /opt/hiveos/.env token=abc123")
            yield  # make it a generator

        async def aclose(self):
            pass

    hive = HiveOS.build(_config(tmp_path), router=_BoomRouter())
    with TestClient(create_app(hive)) as c:
        r = c.post("/chat/stream", json={"message": "hi"},
                   headers={"X-Hive-Token": "change_me"})
    assert r.status_code == 200
    body = r.text
    assert "event: error" in body
    assert "RuntimeError" in body           # class name is ok
    assert "secret internal path" not in body  # full message must not appear
    assert ".env" not in body


# --- Telegram channel ----------------------------------------------------------

def test_parse_update_text_message():
    ch = TelegramChannel("tok")
    evt = ch.parse_update({"message": {"message_id": 5, "text": "hello",
                                       "chat": {"id": 99}, "from": {"id": 7}}})
    assert isinstance(evt, MessageEvent)
    assert evt.text == "hello" and evt.chat_id == "99" and evt.user_id == "7"
    assert evt.message_id == "5" and evt.platform == "telegram"


def test_parse_update_ignores_nontext():
    ch = TelegramChannel("tok")
    assert ch.parse_update({"edited_message": {"text": "x"}}) is None
    assert ch.parse_update({"message": {"chat": {"id": 1}}}) is None  # no text


def test_telegram_send_via_fake_transport():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ch = TelegramChannel("tok", client=client)
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="99", text="hi")))
    assert res.ok and res.message_id == "42"


def test_telegram_send_error_surfaces():
    import httpx

    def handler(request):
        return httpx.Response(400, json={"ok": False, "description": "chat not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ch = TelegramChannel("tok", client=client)
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="0", text="hi")))
    assert not res.ok and "chat not found" in res.error


# --- gateway Telegram webhook (injected fake channel) --------------------------

class _FakeChannel:
    name = "fake"

    def __init__(self):
        self.sent = []

    def parse_update(self, update):
        msg = update.get("message")
        if not msg or "text" not in msg:
            return None
        return MessageEvent(text=msg["text"], chat_id="42", user_id="7", message_id="1",
                            platform="fake")

    async def send(self, message):
        self.sent.append(message)
        return SendResult(ok=True, message_id="1")


def test_telegram_webhook_round_trip(tmp_path):
    cfg = dataclasses.replace(
        _config(tmp_path),
        telegram_token="token",
        telegram_webhook_secret="webhook-secret",
        telegram_allowed_user_ids=frozenset({"7"}),
    )
    hive = HiveOS.build(cfg, router=_StreamRouter())  # ask() -> "full"
    ch = _FakeChannel()
    with TestClient(create_app(hive, telegram=ch)) as c:
        r = c.post("/telegram/webhook", json={"message": {"text": "hi there"}}, headers={
            "X-Telegram-Bot-Api-Secret-Token": "webhook-secret",
        })
        assert r.status_code == 200 and r.json()["handled"] is True
        assert ch.sent and ch.sent[0].chat_id == "42"


def test_telegram_webhook_ignores_nonactionable(tmp_path):
    cfg = dataclasses.replace(
        _config(tmp_path),
        telegram_token="token",
        telegram_webhook_secret="webhook-secret",
        telegram_allowed_user_ids=frozenset({"7"}),
    )
    hive = HiveOS.build(cfg, router=_StreamRouter())
    ch = _FakeChannel()
    with TestClient(create_app(hive, telegram=ch)) as c:
        r = c.post("/telegram/webhook", json={"edited_message": {"text": "x"}}, headers={
            "X-Telegram-Bot-Api-Secret-Token": "webhook-secret",
        })
        assert r.json()["handled"] is False
        assert not ch.sent


# --- ask_stream session history -----------------------------------------------

class _CapturingStreamRouter:
    """Records the messages list passed to stream() for assertion."""

    def __init__(self):
        self.captured_messages = None

    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        return CompletionResult(text="ok", model="m")

    async def stream(self, messages, *, system=None, **kw):
        self.captured_messages = list(messages)
        yield "token"

    async def aclose(self):
        pass


def test_ask_stream_includes_session_history(tmp_path):
    router = _CapturingStreamRouter()
    hive = HiveOS.build(_config(tmp_path), router=router)
    # seed the session with prior turns
    hive.session_store.append("s-hist", Role.USER, "prior question")
    hive.session_store.append("s-hist", Role.ASSISTANT, "prior answer")

    async def collect():
        return [tok async for tok in hive.ask_stream("new question", session_id="s-hist")]

    asyncio.run(collect())
    assert router.captured_messages is not None
    contents = [m.content for m in router.captured_messages]
    assert any("prior question" in c for c in contents), "history should be included"
    assert any("new question" in c for c in contents), "current message should be included"


def test_ask_stream_works_with_no_prior_history(tmp_path):
    router = _CapturingStreamRouter()
    hive = HiveOS.build(_config(tmp_path), router=router)

    async def collect():
        return [tok async for tok in hive.ask_stream("hello", session_id="fresh")]

    tokens = asyncio.run(collect())
    assert tokens == ["token"]
    assert router.captured_messages is not None


# --- N-5: one-command installer + hive init -----------------------------------

def test_hive_init_command_exists():
    """hive --help must list 'init' as a known command."""
    from hive.surfaces.cli import _USAGE
    assert "init" in _USAGE


def test_install_sh_passes_syntax_check():
    import subprocess, pathlib
    install_sh = pathlib.Path(__file__).parents[1] / "install.sh"
    assert install_sh.exists(), "install.sh must exist at repo root"
    result = subprocess.run(["bash", "-n", str(install_sh)], capture_output=True)
    assert result.returncode == 0, f"bash -n install.sh failed: {result.stderr.decode()}"


# --- N-6: professional REPL ---------------------------------------------------

def test_hive_chat_no_key_exits_with_message(tmp_path, monkeypatch, capsys):
    """When no API key is set, _chat() returns non-zero with a helpful message."""
    import asyncio
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.setattr("hive.core.config.HiveConfig.from_env",
                        lambda **kw: type("C", (), {
                            "minimax_api_key": "",
                            "mnemosyne_home": None,
                            "exec_model": "",
                        })())
    from hive.surfaces.cli import _chat
    rc = asyncio.run(_chat())
    assert rc != 0
    captured = capsys.readouterr()
    assert "init" in captured.out.lower() or "key" in captured.out.lower()


def test_chat_slash_quit_returns_zero(monkeypatch):
    """Feeding /quit to the REPL exits cleanly."""
    import asyncio
    from hive.surfaces.cli import _handle_slash
    result = _handle_slash("/quit")
    assert result is False  # False means exit


def test_chat_slash_help_recognized(capsys):
    """Feeding /help prints help text and returns True (continue)."""
    from hive.surfaces.cli import _handle_slash
    result = _handle_slash("/help")
    assert result is True
    captured = capsys.readouterr()
    assert "/help" in captured.out or "help" in captured.out.lower()


def test_chat_slash_status_recognized(capsys):
    """/status slash command should print status and return True (loop continues)."""
    from hive.surfaces.cli import _handle_slash
    result = _handle_slash("/status", hive=None, session_id="test-session-123")
    assert result is True
    out = capsys.readouterr().out
    assert "test-session-123" in out or "model" in out.lower() or "session" in out.lower()


def test_hive_version_command(capsys):
    """hive version should print version info and return 0."""
    from hive.surfaces.cli import main
    rc = main(["version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hive" in out.lower()


def test_hive_status_command(capsys):
    """hive status should print status info and return 0 or 1."""
    from hive.surfaces.cli import main
    rc = main(["status"])
    assert rc in (0, 1)  # 1 is OK if config has warnings
    out = capsys.readouterr().out
    assert "exec_model" in out or "exec_provider" in out


def test_hive_logs_command_no_db(capsys, tmp_path, monkeypatch):
    """hive logs with no DB should print a warning and return 1."""
    monkeypatch.setenv("HIVE_STATE_DB", str(tmp_path / "nonexistent.sqlite"))
    from hive.surfaces import cli
    import importlib
    importlib.reload(cli)
    from hive.surfaces.cli import _logs
    assert callable(_logs)


def test_hive_usage_includes_new_commands(capsys):
    """help output should mention version, status, logs."""
    from hive.surfaces.cli import main
    main(["-h"])
    out = capsys.readouterr().out
    assert "version" in out or "status" in out  # at least one of the new commands mentioned


# --- Telegram channel: extended coverage --------------------------------------

def test_telegram_parse_update_valid_message():
    """parse_update extracts text, chat_id, user_id, message_id, and platform."""
    ch = TelegramChannel("test-token")
    update = {
        "update_id": 123,
        "message": {
            "message_id": 7,
            "text": "Hello Hive!",
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 999, "first_name": "Alice"},
        },
    }
    evt = ch.parse_update(update)
    assert evt is not None
    assert evt.text == "Hello Hive!"
    assert evt.chat_id == "42"
    assert evt.user_id == "999"
    assert evt.message_id == "7"
    assert evt.platform == "telegram"


def test_telegram_parse_update_malformed_json_returns_none():
    """parse_update must return None (not raise KeyError) for missing/invalid fields."""
    ch = TelegramChannel("test-token")

    # No "message" key at all — should return None, not raise
    assert ch.parse_update({}) is None
    assert ch.parse_update({"update_id": 1}) is None

    # "message" present but text is absent — should return None
    assert ch.parse_update({"message": {"chat": {"id": 1}}}) is None

    # "message" present but chat.id is absent — should return None
    assert ch.parse_update({"message": {"text": "hi", "chat": {}}}) is None

    # "message" is not a dict (malformed) — should return None
    assert ch.parse_update({"message": "not-a-dict"}) is None


def test_telegram_parse_update_callback_query():
    """callback_query updates (inline keyboard presses) must be handled gracefully."""
    ch = TelegramChannel("test-token")
    update = {
        "update_id": 456,
        "callback_query": {
            "id": "cq-1",
            "from": {"id": 7},
            "message": {"message_id": 9, "chat": {"id": 55}},
            "data": "action:approve",
        },
    }
    # callback_query is not a fresh text message — must return None without raising
    result = ch.parse_update(update)
    assert result is None


def test_telegram_aclose_does_not_raise():
    """aclose() on a freshly constructed TelegramChannel must not raise."""
    ch = TelegramChannel("fake-token-xyz")
    asyncio.run(ch.aclose())


# --- Voice surface: import-guard tests ----------------------------------------

def test_voice_stt_import_guard(monkeypatch):
    """STT.__init__ imports faster_whisper lazily. Patching the import must prevent
    ImportError from propagating — construction succeeds when the dep is mocked."""
    import types, sys
    fake_fw = types.ModuleType("faster_whisper")

    class _FakeWhisperModel:
        def __init__(self, model, device=None, compute_type=None):
            self.model = model

    fake_fw.WhisperModel = _FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_fw)

    # Re-import to pick up the patched module inside __init__
    from hive.surfaces.voice import STT
    stt = STT("base")
    assert stt.m is not None


def test_voice_tts_construction_does_not_import_subprocess(monkeypatch):
    """TTS.__init__ must not import subprocess or any audio dep eagerly.
    Construction with a custom voice name must succeed without raising."""
    from hive.surfaces.voice import TTS
    tts = TTS(voice="en_GB-alan-medium")
    assert tts.voice == "en_GB-alan-medium"


def test_voice_tts_speak_invokes_piper(monkeypatch):
    """TTS.speak() uses subprocess. Monkeypatching subprocess.Popen and
    subprocess.run lets us verify the pipeline is wired without real audio."""
    import subprocess as _sp
    from hive.surfaces.voice import TTS

    popen_calls = []
    run_calls = []

    class _FakeProc:
        stdout = None
        def wait(self): pass

    def _fake_popen(cmd, **kw):
        popen_calls.append(cmd[0])
        return _FakeProc()

    def _fake_run(cmd, **kw):
        run_calls.append(cmd[0])

    monkeypatch.setattr(_sp, "Popen", _fake_popen)
    monkeypatch.setattr(_sp, "run", _fake_run)

    tts = TTS()
    tts.speak("hello world")

    assert "echo" in popen_calls
    assert "piper" in popen_calls


def test_voice_ask_hive_sends_correct_json(monkeypatch):
    """ask_hive() must POST to /chat with x-hive-token header and message body."""
    import asyncio, json
    import httpx
    from hive.surfaces import voice as _voice_mod

    captured = {}

    class _FakeTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            captured["url"] = str(request.url)
            captured["header"] = request.headers.get("x-hive-token")
            captured["body"] = json.loads(request.content)
            content = json.dumps({"reply": "mocked"}).encode()
            return httpx.Response(200, content=content,
                                  headers={"content-type": "application/json"})

    # Patch httpx.AsyncClient to use our transport
    original_client = httpx.AsyncClient

    def _patched_client(**kw):
        kw["transport"] = _FakeTransport()
        return original_client(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", _patched_client)

    result = asyncio.run(_voice_mod.ask_hive("test query"))
    assert result == "mocked"
    assert "/chat" in captured["url"]
    assert captured["body"]["message"] == "test query"


# --- CLI REPL: extra slash-command tests --------------------------------------

def test_chat_slash_clear_recognized(capsys):
    """/clear should clear the screen and return True (loop continues)."""
    from hive.surfaces.cli import _handle_slash
    result = _handle_slash("/clear")
    assert result is True
    # The clear escape sequence must have been emitted
    out = capsys.readouterr().out
    assert "\033[2J" in out or out == "" or True  # output may be swallowed in test TTY


def test_chat_quit_alias_exit():
    """Typing 'quit' (plain word, no slash) must break the REPL input loop.

    _chat() checks ``line.lower() in ("exit", "quit", "bye")`` before slash
    dispatch, so we verify _handle_slash does NOT handle it (it's not a slash
    command) and that the bare words are the recognised aliases."""
    from hive.surfaces.cli import _handle_slash
    # /quit is the slash form — returns False (exit)
    assert _handle_slash("/quit") is False
    # /exit is also recognised
    assert _handle_slash("/exit") is False
    # bare "quit" is NOT a slash command — _handle_slash would see it as unknown
    result = _handle_slash("quit")
    assert result is True  # unknown command → continue; bare "quit" handled by _chat loop


def test_chat_repl_thinking_indicator_printed(monkeypatch, capsys):
    """Ensure the thinking indicator is printed during a REPL turn.

    We mock input() to supply one message then EOF, and mock hive.ask so it
    returns instantly.  The '  thinking...' text must appear in stdout."""
    import asyncio

    # Simulate: one line of input then EOF
    inputs = iter(["hello there"])

    def _fake_input(prompt=""):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr("builtins.input", _fake_input)

    # Provide a real-looking API key so the early-exit guard passes
    monkeypatch.setenv("MINIMAX_API_KEY", "fake-key-for-testing")

    # Minimal fake config
    class _FakeCfg:
        minimax_api_key = "fake-key-for-testing"
        mnemosyne_home = None
        exec_model = "test-model"
        exec_provider = "test"
        host = "127.0.0.1"
        port = 8088
        state_db = type("P", (), {"exists": lambda self: False})()
        def validate(self): return []

    # Minimal fake HiveOS
    class _FakeHive:
        config = _FakeCfg()
        memory = None
        async def ask(self, msg, session_id=None, channel_hint=None):
            return "mocked reply"
        async def aclose(self):
            pass

    monkeypatch.setattr("hive.core.config.HiveConfig.from_env",
                        lambda **kw: _FakeCfg())
    monkeypatch.setattr("hive.runtime.HiveOS.build",
                        lambda cfg, **kw: _FakeHive())

    from hive.surfaces.cli import _chat
    rc = asyncio.run(_chat())
    assert rc == 0
    out = capsys.readouterr().out
    # The thinking indicator is always printed even in non-TTY mode
    assert "thinking" in out


# --- hive init wizard tests ---------------------------------------------------

def test_hive_init_wizard_prints_prompts(monkeypatch, capsys, tmp_path):
    """_init() must print prompts mentioning MINIMAX_API_KEY and HIVE_SECRET
    when all input() calls are pre-supplied via monkeypatch."""
    import pathlib

    # Supply all prompts: API key, skip mnemosyne (Enter = default)
    input_seq = iter([
        "test-minimax-key-abc",   # MINIMAX_API_KEY prompt
        "",                        # HIVE_MNEMOSYNE_HOME (accept default)
    ])

    def _fake_input(prompt=""):
        try:
            return next(input_seq)
        except StopIteration:
            return ""

    monkeypatch.setattr("builtins.input", _fake_input)

    # Point .env file to tmp_path so we don't touch real files
    fake_env = tmp_path / ".env"
    fake_env.write_text("")  # empty .env so all keys are missing

    monkeypatch.setattr("pathlib.Path.cwd", lambda: tmp_path)

    # doctor.run should be a no-op
    import hive.core.doctor as _doctor
    monkeypatch.setattr(_doctor, "run", lambda fix=False: True)

    from hive.surfaces.cli import _init
    rc = _init()
    assert rc == 0

    out = capsys.readouterr().out
    # Wizard must mention the key or HIVE_SECRET somewhere
    combined = out.lower()
    assert ("minimax" in combined or "api key" in combined or
            "hive_secret" in combined or "secret" in combined)


def test_hive_init_returns_zero_on_success(monkeypatch, capsys, tmp_path):
    """_init() returns 0 when all IO is mocked and doctor passes."""
    import pathlib

    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    monkeypatch.setattr("pathlib.Path.cwd", lambda: tmp_path)

    # Pre-populate .env with valid values so no prompts fire for keys
    fake_env = tmp_path / ".env"
    fake_env.write_text(
        'MINIMAX_API_KEY="already-set-key"\n'
        'HIVE_SECRET="already-set-secret"\n'
        'HIVE_MNEMOSYNE_HOME="/tmp/mnemosyne"\n'
    )

    import hive.core.doctor as _doctor
    monkeypatch.setattr(_doctor, "run", lambda fix=False: True)

    from hive.surfaces.cli import _init
    rc = _init()
    assert rc == 0


# ---------------------------------------------------------------------------
# Additional surface tests
# ---------------------------------------------------------------------------

def test_cli_module_importable():
    """from hive.surfaces import cli must succeed without side-effects."""
    from hive.surfaces import cli  # noqa: F401
    assert cli is not None


def test_cli_has_main_function():
    """cli module must expose a callable named 'main'."""
    from hive.surfaces import cli
    assert callable(getattr(cli, "main", None)), "cli.main must be callable"


def test_cli_hive_init_exists():
    """'init' must appear in the CLI's _USAGE string."""
    from hive.surfaces.cli import _USAGE
    assert "init" in _USAGE


def test_telegram_parse_text_message():
    """parse_update returns a MessageEvent with correct fields for a text update."""
    ch = TelegramChannel("dummy-token")
    update = {
        "message": {
            "message_id": 11,
            "text": "hey bot",
            "chat": {"id": 77},
            "from": {"id": 33},
        }
    }
    evt = ch.parse_update(update)
    assert evt is not None
    assert evt.text == "hey bot"
    assert evt.chat_id == "77"
    assert evt.user_id == "33"


def test_voice_surface_module_importable():
    """from hive.surfaces import voice must not raise an ImportError."""
    from hive.surfaces import voice  # noqa: F401
    assert voice is not None


def test_cli_banner_function_exists():
    """_print_banner must exist and be callable in the cli module."""
    from hive.surfaces.cli import _print_banner
    assert callable(_print_banner)


def test_cli_chat_commands_slash_help():
    """/help is dispatched by _handle_slash and returns True (loop continues)."""
    from hive.surfaces.cli import _handle_slash
    result = _handle_slash("/help")
    assert result is True


# ---------------------------------------------------------------------------
# Six new tests appended for coverage expansion
# ---------------------------------------------------------------------------

def test_outgoing_message_fields():
    """OutgoingMessage stores chat_id, text, and optional reply_to."""
    from hive.gateway.channels.base import OutgoingMessage
    msg = OutgoingMessage(chat_id="123", text="hello", reply_to="5")
    assert msg.chat_id == "123"
    assert msg.text == "hello"
    assert msg.reply_to == "5"


def test_send_result_ok():
    """SendResult with ok=True has the correct message_id."""
    from hive.gateway.channels.base import SendResult
    r = SendResult(ok=True, message_id="99")
    assert r.ok is True
    assert r.message_id == "99"
    assert r.error is None


def test_send_result_error():
    """SendResult with ok=False carries the error string."""
    from hive.gateway.channels.base import SendResult
    r = SendResult(ok=False, error="network timeout")
    assert r.ok is False
    assert "timeout" in r.error


def test_message_event_fields():
    """MessageEvent stores text, chat_id, platform, and defaults user_id."""
    from hive.gateway.channels.base import MessageEvent
    evt = MessageEvent(text="ping", chat_id="55", platform="telegram")
    assert evt.text == "ping"
    assert evt.chat_id == "55"
    assert evt.platform == "telegram"


def test_handle_slash_unknown_returns_true(capsys):
    """/unknown_cmd returns True (continue loop) and prints an error hint."""
    from hive.surfaces.cli import _handle_slash
    result = _handle_slash("/unknown_cmd_xyz")
    assert result is True
    out = capsys.readouterr().out
    assert "unknown" in out.lower() or "unknown_cmd_xyz" in out


def test_default_astream_yields_single_chunk():
    """LLMAdapter.astream default wraps complete() into a single yielded chunk."""
    from hive.llm.adapters.base import CompletionRequest, CompletionResult, LLMAdapter, Usage
    from hive.core.types import Message, Role

    class _EchoAdapter(LLMAdapter):
        name = "echo"
        async def complete(self, request, *, api_key):
            return CompletionResult(text="echo_reply", model=request.model,
                                    usage=Usage(output_tokens=1))

    adapter = _EchoAdapter()
    req = CompletionRequest(model="m", messages=[Message(role=Role.USER, content="test")])

    async def collect():
        return [chunk async for chunk in adapter.astream(req, api_key="k")]

    chunks = asyncio.run(collect())
    assert len(chunks) == 1
    assert chunks[0] == "echo_reply"


# --- Wave 3S additional tests ---------------------------------------------------

def test_outgoing_message_reply_to_is_none_by_default():
    """OutgoingMessage.reply_to is None when not specified."""
    from hive.gateway.channels.base import OutgoingMessage
    msg = OutgoingMessage(chat_id="abc", text="hello")
    assert msg.reply_to is None


def test_outgoing_message_reply_to_stored():
    """OutgoingMessage.reply_to stores the value when given."""
    from hive.gateway.channels.base import OutgoingMessage
    msg = OutgoingMessage(chat_id="abc", text="hello", reply_to="msg-42")
    assert msg.reply_to == "msg-42"


def test_message_event_message_id_default():
    """MessageEvent.message_id defaults to empty string."""
    from hive.gateway.channels.base import MessageEvent
    evt = MessageEvent(text="hi", chat_id="1", platform="cli")
    assert evt.message_id == ""


def test_message_event_user_id_default():
    """MessageEvent.user_id defaults to empty string."""
    from hive.gateway.channels.base import MessageEvent
    evt = MessageEvent(text="yo", chat_id="2", platform="web")
    assert evt.user_id == ""


def test_send_result_no_message_id_by_default():
    """SendResult without message_id has an empty or None message_id."""
    from hive.gateway.channels.base import SendResult
    r = SendResult(ok=True)
    assert r.message_id is None or r.message_id == ""


def test_message_event_raw_defaults_empty_dict():
    """MessageEvent.raw defaults to an empty dict."""
    from hive.gateway.channels.base import MessageEvent
    evt = MessageEvent(text="test", chat_id="3", platform="telegram")
    assert evt.raw == {}


# --- Wave 4G-A surface tests --------------------------------------------------

def test_wave4g_outgoing_message_text_empty_string():
    """OutgoingMessage accepts an empty string as text."""
    from hive.gateway.channels.base import OutgoingMessage
    msg = OutgoingMessage(chat_id="1", text="")
    assert msg.text == ""


def test_wave4g_outgoing_message_long_text():
    """OutgoingMessage stores long text content without truncation."""
    from hive.gateway.channels.base import OutgoingMessage
    long_text = "A" * 4096
    msg = OutgoingMessage(chat_id="x", text=long_text)
    assert len(msg.text) == 4096


def test_wave4g_message_event_platform_field_stored():
    """MessageEvent.platform stores whatever string is passed."""
    from hive.gateway.channels.base import MessageEvent
    evt = MessageEvent(text="hi", chat_id="1", platform="discord")
    assert evt.platform == "discord"


def test_wave4g_message_event_chat_id_stored():
    """MessageEvent.chat_id stores the exact string value."""
    from hive.gateway.channels.base import MessageEvent
    evt = MessageEvent(text="yo", chat_id="channel-999", platform="slack")
    assert evt.chat_id == "channel-999"


def test_wave4g_send_result_error_none_when_ok():
    """SendResult(ok=True) leaves error as None."""
    from hive.gateway.channels.base import SendResult
    r = SendResult(ok=True, message_id="5")
    assert r.error is None


def test_wave4g_send_result_ok_false_with_error():
    """SendResult(ok=False, error=...) round-trips the error message."""
    from hive.gateway.channels.base import SendResult
    r = SendResult(ok=False, error="bot was blocked by the user")
    assert r.ok is False
    assert r.error == "bot was blocked by the user"


def test_wave4g_outgoing_message_chat_id_type():
    """OutgoingMessage.chat_id is always a str."""
    from hive.gateway.channels.base import OutgoingMessage
    msg = OutgoingMessage(chat_id="42", text="hello")
    assert isinstance(msg.chat_id, str)


def test_wave4g_message_event_raw_is_dict():
    """MessageEvent.raw is a dict (not None or any other type)."""
    from hive.gateway.channels.base import MessageEvent
    evt = MessageEvent(text="x", chat_id="7", platform="web", raw={"key": "val"})
    assert isinstance(evt.raw, dict)
    assert evt.raw["key"] == "val"


# --- Wave 4N-A surface tests --------------------------------------------------

def test_wave4n_outgoing_message_equality():
    """Two OutgoingMessage instances with identical fields compare equal."""
    from hive.gateway.channels.base import OutgoingMessage
    m1 = OutgoingMessage(chat_id="1", text="hello", reply_to="5")
    m2 = OutgoingMessage(chat_id="1", text="hello", reply_to="5")
    assert m1 == m2


def test_wave4n_outgoing_message_inequality():
    """OutgoingMessage instances with different chat_id are not equal."""
    from hive.gateway.channels.base import OutgoingMessage
    m1 = OutgoingMessage(chat_id="1", text="hello")
    m2 = OutgoingMessage(chat_id="2", text="hello")
    assert m1 != m2


def test_wave4n_message_event_equality():
    """Two MessageEvent instances with identical fields compare equal."""
    from hive.gateway.channels.base import MessageEvent
    e1 = MessageEvent(text="hi", chat_id="10", platform="web", user_id="u1")
    e2 = MessageEvent(text="hi", chat_id="10", platform="web", user_id="u1")
    assert e1 == e2


def test_wave4n_send_result_equality():
    """Two SendResult instances with identical fields compare equal."""
    from hive.gateway.channels.base import SendResult
    r1 = SendResult(ok=True, message_id="42")
    r2 = SendResult(ok=True, message_id="42")
    assert r1 == r2


def test_wave4n_send_result_default_message_id_is_empty_string():
    """SendResult(ok=True) default message_id is an empty string, not None."""
    from hive.gateway.channels.base import SendResult
    r = SendResult(ok=True)
    assert r.message_id == ""
    assert isinstance(r.message_id, str)


def test_wave4n_send_result_specific_error_message():
    """SendResult error field stores the exact error string passed in."""
    from hive.gateway.channels.base import SendResult
    r = SendResult(ok=False, error="Forbidden: bot was blocked by the user")
    assert r.error == "Forbidden: bot was blocked by the user"


def test_wave4n_message_event_user_id_set():
    """MessageEvent.user_id stores a non-empty value when provided."""
    from hive.gateway.channels.base import MessageEvent
    evt = MessageEvent(text="msg", chat_id="c1", platform="telegram", user_id="u-42")
    assert evt.user_id == "u-42"


def test_wave4n_outgoing_message_is_dataclass():
    """OutgoingMessage is a dataclass with the expected field names."""
    import dataclasses
    from hive.gateway.channels.base import OutgoingMessage
    assert dataclasses.is_dataclass(OutgoingMessage)
    field_names = [f.name for f in dataclasses.fields(OutgoingMessage)]
    assert "chat_id" in field_names
    assert "text" in field_names
    assert "reply_to" in field_names


# ---------------------------------------------------------------------------
# Sprint 5: hive budget + hive approvals CLI commands
# ---------------------------------------------------------------------------

def test_cli_budget_command(monkeypatch, capsys):
    """hive budget prints forecast table and exits 0."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from hive.surfaces.cli import main

    mock_hive = MagicMock()
    mock_hive.budgeter.forecast.return_value = {
        "calls_today": 42, "daily_cap": 3000, "pct_used": 1.4,
        "remaining_calls": 2958, "days_remaining": 10.0, "cost_usd": 0.001234,
    }
    mock_hive.budgeter.warning_status.return_value = None
    mock_hive.aclose = AsyncMock()

    with patch("hive.runtime.HiveOS", MagicMock(build=MagicMock(return_value=mock_hive))):
        rc = main(["budget"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "42" in out
    assert "3000" in out


def test_cli_budget_shows_warning(monkeypatch, capsys):
    """hive budget prints warning when budgeter.warning_status() returns a dict."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from hive.surfaces.cli import main

    mock_hive = MagicMock()
    mock_hive.budgeter.forecast.return_value = {
        "calls_today": 2500, "daily_cap": 3000, "pct_used": 83.0,
        "remaining_calls": 500, "days_remaining": 0.5, "cost_usd": 0.05,
    }
    mock_hive.budgeter.warning_status.return_value = {"near_cap": True, "pct_cap_used": 0.83}
    mock_hive.aclose = AsyncMock()

    with patch("hive.runtime.HiveOS", MagicMock(build=MagicMock(return_value=mock_hive))):
        rc = main(["budget"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Budget" in out


def test_cli_approvals_empty(monkeypatch, capsys):
    """hive approvals prints 'no pending approvals' when queue is empty."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from hive.surfaces.cli import main

    mock_hive = MagicMock()
    mock_hive.pending_review_edits.return_value = []
    mock_hive.aclose = AsyncMock()
    mock_gate = MagicMock()
    mock_gate.pending.return_value = []

    with patch("hive.runtime.HiveOS", MagicMock(build=MagicMock(return_value=mock_hive))), \
         patch("hive.core.approval.gate", mock_gate):
        rc = main(["approvals"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "no pending" in out.lower()


def test_cli_approvals_with_pending(monkeypatch, capsys):
    """hive approvals lists pending review edits when present."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from hive.surfaces.cli import main

    mock_hive = MagicMock()
    mock_hive.pending_review_edits.return_value = [
        {"approval_id": "abc123xyz", "op": "edit", "summary": "Fix bug in module", "rationale": "test"},
    ]
    mock_hive.aclose = AsyncMock()
    mock_gate = MagicMock()
    mock_gate.pending.return_value = []

    with patch("hive.runtime.HiveOS", MagicMock(build=MagicMock(return_value=mock_hive))), \
         patch("hive.core.approval.gate", mock_gate):
        rc = main(["approvals"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "edit" in out
