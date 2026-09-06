"""
test_cli_commands.py — covers surfaces/cli.py command surface.

Targets the lines missed by tests/test_surfaces.py (banner config branch,
slash commands with live hive object, init wizard, async command handlers,
main() argv routing). Aims to lift cli.py from 69% toward 85%+ line coverage.

Strategy:
- `_print_banner` / `_handle_slash` are pure-Python and easy to drive directly.
- `_init` requires mocking `input()` and the `.env` lookup path.
- `_logs` / `_budget` / `_approvals` are async; we monkeypatch `HiveOS.build`
  and call the inner coroutine via `asyncio.run`.
- `main()` argv routing is fully testable by passing a list, no mocking needed
  beyond the per-command handlers themselves.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from hive.surfaces import cli


# ---------------------------------------------------------------------------
# _print_banner
# ---------------------------------------------------------------------------

class TestPrintBanner:
    def test_no_color_env_emits_plain(self, monkeypatch, capsys):
        monkeypatch.setenv("NO_COLOR", "1")
        cli._print_banner(cfg=None)
        out = capsys.readouterr().out
        assert "HiveOS v" in out
        assert "\033[" not in out  # no ANSI escape codes

    def test_with_config_extracts_memory_and_model(self, monkeypatch, capsys):
        monkeypatch.setenv("NO_COLOR", "1")
        cfg = MagicMock()
        cfg.mnemosyne_home = "/tmp/mnem"
        cfg.exec_model = "anthropic/claude-sonnet-4-6"
        cli._print_banner(cfg=cfg)
        out = capsys.readouterr().out
        assert "mnemosyne" in out
        # [:16] truncates the trailing "6"
        assert "claude-sonnet-4-" in out

    def test_with_empty_exec_model_uses_default(self, monkeypatch, capsys):
        monkeypatch.setenv("NO_COLOR", "1")
        cfg = MagicMock()
        cfg.mnemosyne_home = None
        cfg.exec_model = ""
        cli._print_banner(cfg=cfg)
        out = capsys.readouterr().out
        assert "MiniMax" in out  # default fallback
        assert "local" in out     # default fallback for memory

    def test_tty_branch_uses_ansi_when_color_enabled(self, monkeypatch, capsys):
        # Force isatty() to return True even though pytest captures stdout.
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        cli._print_banner(cfg=None)
        out = capsys.readouterr().out
        # ANSI cyan banner should be present
        assert "\033[36m" in out


# ---------------------------------------------------------------------------
# _handle_slash
# ---------------------------------------------------------------------------

class TestHandleSlash:
    def test_help_prints_and_continues(self, capsys):
        assert cli._handle_slash("/help") is True
        out = capsys.readouterr().out
        assert "/help" in out and "/status" in out and "/quit" in out

    def test_quit_returns_false(self):
        assert cli._handle_slash("/quit") is False
        assert cli._handle_slash("/exit") is False

    def test_status_without_hive_uses_defaults(self, capsys):
        cli._handle_slash("/status", hive=None, session_id="abc")
        out = capsys.readouterr().out
        assert "model=MiniMax" in out
        assert "memory=local" in out
        assert "session=abc" in out

    def test_status_with_hive_uses_live_values(self, capsys):
        hive = MagicMock()
        hive.config.exec_model = "anthropic/claude-fable-5"
        hive.memory.name = "mnemosyne"
        cli._handle_slash("/status", hive=hive, session_id="xyz")
        out = capsys.readouterr().out
        assert "claude-fable-5" in out
        assert "memory=mnemosyne" in out
        assert "session=xyz" in out

    def test_clear_returns_true(self):
        assert cli._handle_slash("/clear") is True

    def test_unknown_command_warns_and_continues(self, capsys):
        assert cli._handle_slash("/nonsense") is True
        out = capsys.readouterr().out
        assert "unknown command" in out
        assert "/nonsense" in out

    def test_empty_input_returns_true(self, capsys):
        assert cli._handle_slash("") is True

    def test_slash_only_no_args(self, capsys):
        assert cli._handle_slash("/") is True
        out = capsys.readouterr().out
        assert "unknown command" in out


# ---------------------------------------------------------------------------
# main() argv routing (pure dispatch)
# ---------------------------------------------------------------------------

class TestMainRouting:
    def test_help_flag(self, capsys):
        assert cli.main(["-h"]) == 0
        assert cli.main(["--help"]) == 0
        assert cli.main(["help"]) == 0
        out = capsys.readouterr().out
        assert "usage: hive" in out

    def test_version(self, capsys):
        with patch.object(cli, "_version", return_value=0) as v:
            assert cli.main(["version"]) == 0
            v.assert_called_once()

    def test_status(self, capsys):
        with patch.object(cli, "_status", return_value=0) as s:
            assert cli.main(["status"]) == 0
            s.assert_called_once()

    def test_logs_default_tail(self, capsys):
        with patch.object(cli, "_logs", return_value=0) as l:
            assert cli.main(["logs"]) == 0
            l.assert_called_once_with(20)

    def test_logs_with_tail_flag(self, capsys):
        with patch.object(cli, "_logs", return_value=0) as l:
            assert cli.main(["logs", "--tail", "5"]) == 0
            l.assert_called_once_with(5)

    def test_logs_with_invalid_tail_falls_back_to_default(self, capsys):
        with patch.object(cli, "_logs", return_value=0) as l:
            assert cli.main(["logs", "--tail", "abc"]) == 0
            l.assert_called_once_with(20)

    def test_selfmod_history_passes_requested_limit(self):
        with patch.object(cli, "_run_async", return_value=0) as run_async:
            assert cli.main(["selfmod-history", "--limit", "7"]) == 0
        assert run_async.call_args.args[0].cr_frame.f_locals["limit"] == 7
        run_async.call_args.args[0].close()

    def test_doctor_without_fix(self):
        with patch("hive.core.doctor.run", return_value=True) as r:
            assert cli.main(["doctor"]) == 0
            r.assert_called_once_with(fix=False)

    def test_doctor_with_fix_returns_nonzero_on_failure(self):
        with patch("hive.core.doctor.run", return_value=False) as r:
            assert cli.main(["doctor", "--fix"]) == 1
            r.assert_called_once_with(fix=True)

    def test_init(self):
        with patch.object(cli, "_init", return_value=0) as i:
            assert cli.main(["init"]) == 0
            i.assert_called_once()

    def test_serve(self):
        with patch.object(cli, "_serve", return_value=0) as s:
            assert cli.main(["serve"]) == 0
            s.assert_called_once()

    def test_heartbeat_uses_run_async(self):
        with patch.object(cli, "_run_async", return_value=0) as ra:
            assert cli.main(["heartbeat"]) == 0
            ra.assert_called_once()
            assert ra.call_args.args[0].__name__ == "_heartbeat"

    def test_consolidate_uses_run_async(self):
        with patch.object(cli, "_run_async", return_value=0) as ra:
            assert cli.main(["consolidate"]) == 0
            assert ra.call_args.args[0].__name__ == "_consolidate"

    def test_mcp_serve_uses_run_async(self):
        with patch.object(cli, "_run_async", return_value=0) as ra:
            assert cli.main(["mcp-serve"]) == 0
            assert ra.call_args.args[0].__name__ == "_mcp_serve"

    def test_budget_uses_run_async(self):
        with patch.object(cli, "_run_async", return_value=0) as ra:
            assert cli.main(["budget"]) == 0
            assert ra.call_args.args[0].__name__ == "_budget"

    def test_approvals_uses_run_async(self):
        with patch.object(cli, "_run_async", return_value=0) as ra:
            assert cli.main(["approvals"]) == 0
            assert ra.call_args.args[0].__name__ == "_approvals"

    def test_ask_without_message_returns_2(self, capsys):
        assert cli.main(["ask"]) == 2
        err = capsys.readouterr().err
        assert "usage: hive ask" in err

    def test_ask_with_message_invokes_async(self):
        with patch.object(cli, "_run_async", return_value=0) as ra:
            assert cli.main(["ask", "hello"]) == 0
            coro = ra.call_args.args[0]
            assert coro.__name__ == "_ask"

    def test_ask_with_multiple_words_joins(self):
        with patch.object(cli, "_run_async", return_value=0) as ra:
            cli.main(["ask", "hello", "world", "again"])
            coro = ra.call_args.args[0]
            assert coro.__name__ == "_ask"

    def test_chat_uses_run_async(self):
        with patch.object(cli, "_run_async", return_value=0) as ra:
            assert cli.main(["chat"]) == 0
            assert ra.call_args.args[0].__name__ == "_chat"

    def test_unknown_command_prints_usage_and_returns_2(self, capsys):
        assert cli.main(["totally-not-a-command"]) == 2
        out_err = capsys.readouterr().err
        assert "unknown command" in out_err
        assert "usage: hive" in out_err

    def test_main_with_no_args_defaults_to_chat(self):
        with patch.object(cli, "_run_async", return_value=0) as ra:
            assert cli.main([]) == 0
            assert ra.call_args.args[0].__name__ == "_chat"

    def test_main_accepts_explicit_argv(self):
        with patch.object(cli, "_version", return_value=0) as v, \
             patch.object(cli, "_status", return_value=0) as s:
            assert cli.main(["version"]) == 0
            v.assert_called_once()
            # also pass argv explicitly
            assert cli.main(argv=["status"]) == 0
            s.assert_called_once()


# ---------------------------------------------------------------------------
# _logs command (sync, DB-backed)
# ---------------------------------------------------------------------------

class TestLogsCommand:
    def test_no_state_db_returns_1_with_hint(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HIVE_STATE_DB", str(tmp_path / "missing.sqlite"))
        assert cli._logs(tail=5) == 1
        out = capsys.readouterr().out
        assert "No state database found" in out
        assert "hive doctor --fix" in out

    def test_db_with_no_audit_log_table(self, tmp_path, monkeypatch, capsys):
        db = tmp_path / "state.sqlite"
        # Create empty DB — no tables.
        sqlite3.connect(str(db)).close()
        monkeypatch.setenv("HIVE_STATE_DB", str(db))
        assert cli._logs(tail=5) == 0
        out = capsys.readouterr().out
        assert "audit_log table not yet created" in out

    def test_db_with_entries(self, tmp_path, monkeypatch, capsys):
        db = tmp_path / "state.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE audit_log (ts REAL, level TEXT, event TEXT, detail TEXT)"
        )
        # Two entries — newest first when ordered DESC.
        conn.execute(
            "INSERT INTO audit_log VALUES (?, ?, ?, ?)",
            (200.0, "INFO", "tool_call", "shell: ls"),
        )
        conn.execute(
            "INSERT INTO audit_log VALUES (?, ?, ?, ?)",
            (100.0, "WARN", "rate_limit", "approaching cap"),
        )
        conn.commit()
        conn.close()
        monkeypatch.setenv("HIVE_STATE_DB", str(db))
        assert cli._logs(tail=10) == 0
        out = capsys.readouterr().out
        assert "tool_call" in out
        assert "rate_limit" in out
        # INFO in green, WARN in yellow (color codes absent due to NO_COLOR).
        assert "INFO" in out and "WARN" in out

    def test_db_with_empty_audit_log(self, tmp_path, monkeypatch, capsys):
        db = tmp_path / "state.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE audit_log (ts REAL, level TEXT, event TEXT, detail TEXT)")
        conn.commit()
        conn.close()
        monkeypatch.setenv("HIVE_STATE_DB", str(db))
        assert cli._logs(tail=10) == 0
        out = capsys.readouterr().out
        assert "no audit entries yet" in out


# ---------------------------------------------------------------------------
# Async command handlers (budget / approvals / ask)
# ---------------------------------------------------------------------------

def _run(coro):
    """Helper: run an awaitable to completion in tests.

    Uses ``asyncio.run`` (not the deprecated ``asyncio.get_event_loop``)
    so the helper works regardless of whether a previous test left an
    event loop lying around on the main thread.
    """
    return asyncio.run(coro)


class TestBudgetCommand:
    def test_budget_healthy(self, capsys):
        forecast = {
            "calls_today": 5,
            "daily_cap": 100,
            "pct_used": 5.0,
            "remaining_calls": 95,
            "days_remaining": 30.0,
            "cost_usd": 0.001234,
        }
        fake_hive = MagicMock()
        fake_hive.aclose = AsyncMock()
        fake_hive.budgeter.forecast.return_value = forecast
        fake_hive.budgeter.warning_status.return_value = None
        fake_hive.config = MagicMock()
        fake_hive.config.mnemosyne_home = Path("/tmp/mnem")
        with patch("hive.runtime.HiveOS.build", return_value=fake_hive):
            rc = _run(cli._budget())
        assert rc == 0
        out = capsys.readouterr().out
        assert "5 / 100" in out
        assert "Budget: OK" in out
        fake_hive.aclose.assert_awaited_once()

    def test_budget_with_warning(self, capsys):
        forecast = {
            "calls_today": 95,
            "daily_cap": 100,
            "pct_used": 95.0,
            "remaining_calls": 5,
            "days_remaining": None,
            "cost_usd": 0.0,
        }
        fake_hive = MagicMock()
        fake_hive.aclose = AsyncMock()
        fake_hive.budgeter.forecast.return_value = forecast
        fake_hive.budgeter.warning_status.return_value = {"level": "warn", "msg": "near cap"}
        with patch("hive.runtime.HiveOS.build", return_value=fake_hive):
            rc = _run(cli._budget())
        assert rc == 0
        out = capsys.readouterr().out
        assert "Budget warning" in out


class TestApprovalsCommand:
    def test_no_pending(self, capsys):
        fake_hive = MagicMock()
        fake_hive.aclose = AsyncMock()
        fake_hive.pending_review_edits.return_value = []
        with patch("hive.runtime.HiveOS.build", return_value=fake_hive), \
             patch("hive.core.approval.gate.pending", return_value=[]):
            rc = _run(cli._approvals())
        assert rc == 0
        out = capsys.readouterr().out
        assert "no pending approvals" in out

    def test_with_pending_edits_and_gate_items(self, capsys):
        fake_hive = MagicMock()
        fake_hive.aclose = AsyncMock()
        fake_hive.pending_review_edits.return_value = [
            {"approval_id": "abc12345deadbeef", "op": "edit", "summary": "fix cli.py"},
        ]
        gate_items = [
            {"approval_id": "feed1234cafebabe", "tool": "deploy", "args": {"target": "gateway"}},
        ]
        with patch("hive.runtime.HiveOS.build", return_value=fake_hive), \
             patch("hive.core.approval.gate.pending", return_value=gate_items):
            rc = _run(cli._approvals())
        assert rc == 0
        out = capsys.readouterr().out
        assert "Self-mod edits" in out
        assert "abc12345" in out
        assert "Gated tool calls" in out
        assert "feed1234" in out
        assert "deploy" in out


class TestAskCommand:
    def test_ask_invokes_hive_and_prints(self, capsys):
        fake_hive = MagicMock()
        fake_hive.aclose = AsyncMock()

        async def _fake_ask(message, **_kwargs):
            return f"echo: {message}"

        fake_hive.ask = _fake_ask
        with patch("hive.runtime.HiveOS.build", return_value=fake_hive):
            rc = _run(cli._ask("hello there"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "echo: hello there" in out
        fake_hive.aclose.assert_awaited_once()
