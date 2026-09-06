"""cli surface — terminal + `hive` entry point.

Thin console for the assembled HiveOS:
  hive              interactive REPL (alias: hive chat)
  hive chat         interactive REPL
  hive init         first-time setup wizard
  hive serve        run the FastAPI gateway
  hive doctor [--fix] environment health checks
  hive ask "<msg>"  one-shot turn, prints the reply
  hive mcp-serve    serve Hive's tool registry as an MCP stdio server

Rendering flows through `get_output()` (Output singleton). Argument parsing
flows through `parser.parse()` → `registry.REGISTRY[cmd].handler(args)`.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from . import parser as _parser_mod
from . import registry as _registry_mod
from . import style as _style_mod  # noqa: F401 — re-exported for `from hive.surfaces.cli import style`

# ---------------------------------------------------------------------------
# Thin ANSI helpers — back-compat for tests/test_surfaces.py imports.
# Prefer `get_output()` for new code; these stay as wrappers around style.
# ---------------------------------------------------------------------------

def _ansi(code: str, text: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _cyan(t: str) -> str:   return _ansi("36", t)
def _green(t: str) -> str:  return _ansi("32", t)
def _yellow(t: str) -> str: return _ansi("33", t)
def _bold(t: str) -> str:   return _ansi("1", t)
def _dim(t: str) -> str:    return _ansi("2", t)


# ---------------------------------------------------------------------------
# Banner — printed once on `hive chat` startup
# ---------------------------------------------------------------------------

_BANNER = r"""
 ██╗  ██╗██╗██╗   ██╗███████╗
 ██║  ██║██║██║   ██║██╔════╝
 ███████║██║██║   ██║█████╗
 ██╔══██║██║╚██╗ ██╔╝██╔══╝
 ██║  ██║██║ ╚████╔╝ ███████╗
 ╚═╝  ╚═╝╚═╝  ╚═══╝  ╚══════╝  OS
"""


def _print_banner(cfg=None) -> None:
    version = "0.3.0"
    try:
        from importlib.metadata import version as _v
        version = _v("hive")
    except Exception:
        pass

    memory_status = "local"
    model_name = "MiniMax"
    if cfg is not None:
        if getattr(cfg, "mnemosyne_home", None):
            memory_status = "mnemosyne"
        exec_model = getattr(cfg, "exec_model", None) or ""
        if exec_model:
            model_name = exec_model.split("/")[-1][:16]

    if not os.environ.get("NO_COLOR") and sys.stdout.isatty():
        print(_cyan(_BANNER))
        print(_bold(f"  Model: {model_name}") + _dim(f"  │  Memory: {memory_status}  │  v{version}"))
        print(_dim("  Type your message or /help · Ctrl-D to exit\n"))
    else:
        print(f"HiveOS v{version}  │  Model: {model_name}  │  Memory: {memory_status}")
        print("Type your message or /help. Ctrl-D to exit.\n")


# ---------------------------------------------------------------------------
# Slash-command handler
# ---------------------------------------------------------------------------

_SLASH_HELP = """
  /help    — show this help
  /status  — show model, memory, session info
  /clear   — clear the screen
  /quit    — exit the REPL
"""

def _handle_slash(cmd: str, hive=None, session_id: str = "") -> bool:
    """Handle /command. Returns True if handled (loop should continue), False to exit."""
    parts = cmd.strip().split()
    name = parts[0].lower() if parts else ""
    if name == "/help":
        print(_SLASH_HELP)
        return True
    if name == "/status":
        model = "MiniMax"
        memory = "local"
        if hive is not None:
            model = getattr(getattr(hive, "config", None), "exec_model", None) or "MiniMax"
            memory = getattr(getattr(hive, "memory", None), "name", "local")
        print(_dim(f"  model={model}  memory={memory}  session={session_id or '(none)'}"))
        return True
    if name == "/clear":
        print("\033[2J\033[H", end="")
        return True
    if name in ("/quit", "/exit"):
        return False
    print(_yellow(f"  unknown command: {name!r}  (try /help)"))
    return True


# ---------------------------------------------------------------------------
# Chat REPL
# ---------------------------------------------------------------------------

async def _chat() -> int:
    from hive.core.config import HiveConfig
    from hive.runtime import HiveOS

    cfg = HiveConfig.from_env()

    api_key = getattr(cfg, "minimax_api_key", "") or os.environ.get("MINIMAX_API_KEY", "")
    if not api_key or api_key in ("YOUR_KEY_HERE", "your-key-here", ""):
        print(_yellow("  No API key configured. Run: ") + _bold("hive init"))
        return 1

    hive = HiveOS.build(cfg)
    _print_banner(cfg)

    import uuid
    session_id = str(uuid.uuid4())

    try:
        while True:
            try:
                line = input(_green("you> ")).strip()
            except EOFError:
                print()
                break
            if not line:
                continue
            if line.lower() in ("exit", "quit", "bye"):
                break
            if line.startswith("/"):
                if not _handle_slash(line, hive=hive, session_id=session_id):
                    break
                continue
            print(_dim("  thinking..."), end="\r", flush=True)
            reply = await hive.ask(line, session_id=session_id, channel_hint="cli")
            print(" " * 14 + "\r", end="")
            print(_cyan("hive> ") + str(reply))
    finally:
        await hive.aclose()
    return 0


# ---------------------------------------------------------------------------
# `hive init` — first-time setup wizard
# ---------------------------------------------------------------------------

def _init() -> int:
    """Interactive first-run wizard: set API keys, run doctor, seed memories."""
    import pathlib

    print(_bold("\n  HiveOS — first-time setup\n"))

    env_candidates = [
        pathlib.Path.cwd() / ".env",
        pathlib.Path(__file__).parents[4] / ".env",
    ]
    env_path = next((p for p in env_candidates if p.exists()), env_candidates[0])
    env_example = env_path.parent / ".env.example"

    if not env_path.exists() and env_example.exists():
        import shutil
        shutil.copy(env_example, env_path)
        print(f"  Created {env_path} from .env.example")

    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text().splitlines()

    def _get_env_val(key: str) -> str:
        for line in lines:
            if line.startswith(f"{key}="):
                return line[len(key) + 1:].strip().strip('"').strip("'")
        return os.environ.get(key, "")

    def _set_env_val(key: str, val: str) -> None:
        nonlocal lines
        new_line = f'{key}="{val}"'
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = new_line
                return
        lines.append(new_line)

    changed = False

    current_key = _get_env_val("MINIMAX_API_KEY")
    if not current_key or current_key in ("YOUR_KEY_HERE", "your-key-here"):
        print("  Enter your MiniMax API key (or press Enter to skip):")
        val = input("  MINIMAX_API_KEY> ").strip()
        if val:
            _set_env_val("MINIMAX_API_KEY", val)
            changed = True

    current_secret = _get_env_val("HIVE_SECRET")
    if not current_secret or current_secret in ("change-me", "your-secret-here", ""):
        import secrets as _sec
        new_secret = _sec.token_hex(24)
        print(f"  Generated new HIVE_SECRET: {new_secret[:8]}...")
        _set_env_val("HIVE_SECRET", new_secret)
        changed = True

    current_mnem = _get_env_val("HIVE_MNEMOSYNE_HOME")
    if not current_mnem:
        default_mnem = str(pathlib.Path.home() / ".hive" / "mnemosyne")
        print(f"  Mnemosyne memory path [{default_mnem}] (Enter to use default):")
        val = input("  HIVE_MNEMOSYNE_HOME> ").strip() or default_mnem
        _set_env_val("HIVE_MNEMOSYNE_HOME", val)
        changed = True

    if changed:
        env_path.write_text("\n".join(lines) + "\n")
        print(f"  Saved {env_path}")

    print(_dim("\n  Running hive doctor --fix..."))
    from hive.core import doctor
    doctor.run(fix=True)

    seed_script = pathlib.Path(__file__).parents[4] / "scripts" / "seed_memories.py"
    if seed_script.exists():
        print(_dim("  Seeding identity memories..."))
        import subprocess
        subprocess.run([sys.executable, str(seed_script)], check=False)

    print(_bold("\n  Setup complete! Run: ") + _cyan("hive chat") + "\n")
    return 0


# ---------------------------------------------------------------------------
# Other commands
# ---------------------------------------------------------------------------

def _run_async(coro):
    return asyncio.run(coro)


async def _ask(message: str) -> int:
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        print(await hive.ask(message, channel_hint="cli"))
    finally:
        await hive.aclose()
    return 0


def _serve() -> int:
    import uvicorn

    from hive.gateway.app import create_app
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    uvicorn.run(
        create_app(hive, close_runtime_on_shutdown=True),
        host=hive.config.host,
        port=hive.config.port,
    )
    return 0


async def _heartbeat() -> int:
    from hive.autonomy.heartbeat import Heartbeat
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        await Heartbeat(hive).run()
    finally:
        await hive.aclose()
    return 0


async def _consolidate() -> int:
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        n = await hive.consolidate()
        print(f"consolidated {n} item(s)")
    finally:
        await hive.aclose()
    return 0


async def _mcp_serve() -> int:
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        await hive.serve_mcp()
    finally:
        await hive.aclose()
    return 0


# ---------------------------------------------------------------------------
# `hive version` / `hive status`
# ---------------------------------------------------------------------------

def _version() -> int:
    version = "0.3.0"
    try:
        from importlib.metadata import version as _v
        version = _v("hive")
    except Exception:
        pass
    from hive.core.config import HiveConfig
    cfg = HiveConfig.from_env()
    print(f"hive {version}")
    print(f"  model:    {cfg.exec_model}")
    print(f"  provider: {cfg.exec_provider}")
    print(f"  memory:   {cfg.mnemosyne_home}")
    return 0


def _status() -> int:
    from hive.core.config import HiveConfig
    cfg = HiveConfig.from_env()

    ok = True
    issues = cfg.validate()

    print(_bold("\n  HiveOS Status\n"))
    print(f"  exec_provider : {cfg.exec_provider}")
    print(f"  exec_model    : {cfg.exec_model}")
    print(f"  host:port     : {cfg.host}:{cfg.port}")
    print(f"  state_db      : {cfg.state_db} " + ("(exists)" if cfg.state_db.exists() else "(missing)"))
    print(f"  mnemosyne     : {cfg.mnemosyne_home} " + ("(exists)" if cfg.mnemosyne_home.exists() else "(not created)"))
    print(f"  learning_loop : {'enabled' if cfg.learning_loop_enabled else 'disabled'}")

    if issues:
        ok = False
        print(_yellow("\n  Config warnings:"))
        for issue in issues:
            print(_yellow(f"    • {issue}"))
    else:
        print(_green("\n  Config: OK"))

    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Learning commands (SPRINT_6 P-F)
# ---------------------------------------------------------------------------

def _learning_status(limit: int = 10) -> int:
    from hive.core.config import HiveConfig
    from hive.core.learning import storage
    cfg = HiveConfig.from_env()
    db = str(cfg.state_db)
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    storage.ensure_schema(db)
    counts = storage.count_by_verdict(db)
    print(_bold("\n  Learning loop status\n"))
    print(f"  enabled      : {cfg.learning_loop_enabled}")
    print(f"  eval_timeout : {cfg.learning_eval_timeout}s")
    print(f"  state_db     : {db}")
    print(f"  accept count : {counts.get('accept', 0)}")
    print(f"  reject count : {counts.get('reject', 0)}")
    recent = storage.query_loops(db, limit=max(1, limit))
    if not recent:
        print(_dim("\n  (no loop outcomes recorded yet)"))
        return 0
    print(_bold(f"\n  Recent loops (last {len(recent)}):\n"))
    for o in recent:
        vcol = _green if o.verdict == "accept" else _yellow
        print(f"  {vcol(o.verdict.upper()):>7}  id={o.id:<4} "
              f"pytest={o.pytest_candidate:.2f}/{o.pytest_baseline:.2f}  "
              f"evals={o.evals_candidate:.2f}/{o.evals_baseline:.2f}  "
              f"{_dim('symptom=' + (o.symptom[:40] or ''))}")
    return 0


def _learning_replay(loop_id: int) -> int:
    from hive.core.config import HiveConfig
    from hive.core.learning import storage
    cfg = HiveConfig.from_env()
    db = str(cfg.state_db)
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    storage.ensure_schema(db)
    loops = storage.query_loops(db, limit=1000)
    match = next((o for o in loops if o.id == loop_id), None)
    if match is None:
        print(_yellow(f"\n  No loop found with id={loop_id}"))
        return 1
    print(_bold(f"\n  Loop {match.id} (recorded {match.ts})\n"))
    print(f"  verdict       : {match.verdict}")
    print(f"  symptom       : {match.symptom}")
    print(f"  pytest        : candidate={match.pytest_candidate:.3f} "
          f"baseline={match.pytest_baseline:.3f}")
    print(f"  evals         : candidate={match.evals_candidate:.3f} "
          f"baseline={match.evals_baseline:.3f}")
    print(f"  worktree      : {match.worktree_branch}")
    print(f"  pr_url        : {match.pr_url or '(none)'}")
    print(f"  reject_reason : {match.reject_reason or '(none)'}")
    return 0


def _learning_dispatch(args: list[str]) -> int:
    """Route `hive learning <sub> ...` to status / replay."""
    if not args:
        print(_USAGE)
        return 1
    sub = args[0]
    if sub == "status":
        limit = 10
        for i, a in enumerate(args[1:], 1):
            if a == "--limit" and i < len(args) - 1:
                try:
                    limit = int(args[i + 1])
                except ValueError:
                    pass
        return _learning_status(limit)
    if sub == "replay":
        if len(args) < 2:
            print(_yellow("\n  Usage: hive learning replay <loop_id>"))
            return 1
        try:
            loop_id = int(args[1])
        except ValueError:
            print(_yellow(f"\n  Invalid loop_id: {args[1]}"))
            return 1
        return _learning_replay(loop_id)
    print(_yellow(f"\n  Unknown learning subcommand: {sub}"))
    print("  Try: hive learning status | hive learning replay <id>")
    return 1


# ---------------------------------------------------------------------------
# `hive logs [--tail N]`
# ---------------------------------------------------------------------------

def _logs(tail: int = 20) -> int:
    import datetime
    import sqlite3

    from hive.core.config import HiveConfig
    cfg = HiveConfig.from_env()

    if not cfg.state_db.exists():
        print(_yellow("  No state database found. Run: hive doctor --fix"))
        return 1

    try:
        conn = sqlite3.connect(str(cfg.state_db))
        try:
            rows = conn.execute(
                "SELECT ts, level, event, detail FROM audit_log ORDER BY ts DESC LIMIT ?",
                (tail,)
            ).fetchall()
            if not rows:
                print(_dim("  (no audit entries yet)"))
                return 0
            for ts, level, event, detail in reversed(rows):
                dt = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                level_colored = _green(level) if level == "INFO" else _yellow(level)
                print(f"  {_dim(dt)}  {level_colored}  {event}  {_dim(str(detail or '')[:60])}")
        except sqlite3.OperationalError:
            print(_dim("  (audit_log table not yet created — run: hive doctor --fix)"))
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(_yellow(f"  Could not read logs: {exc}"))
        return 1
    return 0


# ---------------------------------------------------------------------------
# `hive budget` / `hive approvals`
# ---------------------------------------------------------------------------

async def _budget() -> int:
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        fc = hive.budgeter.forecast()
        warn = hive.budgeter.warning_status()
    finally:
        await hive.aclose()

    print(_bold("\n  HiveOS Budget\n"))
    print(f"  calls today   : {fc['calls_today']} / {fc['daily_cap']}")
    print(f"  pct used      : {fc['pct_used']:.1f}%")
    print(f"  remaining     : {fc['remaining_calls']} calls")
    days = fc.get("days_remaining")
    print(f"  days at rate  : {f'{days:.1f}' if days is not None else 'n/a'}")
    cost = fc.get("cost_usd", 0.0)
    print(f"  cost today    : ${cost:.6f}")

    if warn:
        print(_yellow(f"\n  ⚠ Budget warning: {warn}"))
    else:
        print(_green("\n  Budget: OK"))
    return 0


async def _approvals() -> int:
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        pending_edits = hive.pending_review_edits()
        from hive.core.approval import gate as _gate
        pending_gate = _gate.pending()
    finally:
        await hive.aclose()

    print(_bold("\n  HiveOS Pending Approvals\n"))

    if not pending_edits and not pending_gate:
        print(_dim("  (no pending approvals)"))
        return 0

    if pending_edits:
        print(_yellow(f"  Self-mod edits awaiting review ({len(pending_edits)}):"))
        for edit in pending_edits:
            print(f"    [{edit.get('approval_id', '?')[:8]}] "
                  f"{edit.get('op', '?')}  {edit.get('summary', '')}")

    if pending_gate:
        print(_yellow(f"\n  Gated tool calls ({len(pending_gate)}):"))
        for item in pending_gate:
            print(f"    [{str(item.get('approval_id', '?'))[:8]}] "
                  f"{item.get('tool', '?')} — {str(item.get('args', {}))[:60]}")

    return 0


async def _selfmod_history(limit: int = 20) -> int:
    """List durable self-mod proposal outcomes without performing any mutation."""
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        records = hive.self_mod_history(limit=max(1, min(limit, 100)))
    finally:
        await hive.aclose()
    print(_bold("\n  HiveOS Self-modification History\n"))
    if not records:
        print(_dim("  (no self-mod proposals yet)"))
        return 0
    for record in records:
        outcome = _green(str(record.get("outcome", "ok"))) if record.get("ok") else _yellow(
            str(record.get("outcome", "failed"))
        )
        print(f"  {outcome:<12} {record.get('tier', 'auto'):<6} "
              f"{record.get('branch') or '-':<28} {record.get('title', '')}")
    return 0


# ---------------------------------------------------------------------------
# Registry population — every command, declarative.
# ---------------------------------------------------------------------------

def _int_or(default: int):
    def _coerce(value: str) -> int:
        try:
            return int(value)
        except (ValueError, TypeError):
            return default
    return _coerce


# ---------------------------------------------------------------------------
# Categorized help overview (P-J J3) + completion dispatch.
# ---------------------------------------------------------------------------

def _build_help_overview() -> None:
    """Render a categorized help overview (J3).

    Groups CommandSpec entries by `category` and prints colorized tables.
    Pure I/O — no return value.
    """
    from .output import get_output
    out = get_output()
    out.print("usage: hive [chat|init|ask|serve|heartbeat|consolidate|doctor|mcp-serve|version|status|logs|budget|approvals|learning|completion]",
              token="bold cyan")
    out.print("HiveOS terminal surface — REPL, gateway, ops commands.", token="bold cyan")
    out.rule()
    by_cat: dict[str, list] = {}
    for spec in _registry_mod.REGISTRY.values():
        by_cat.setdefault(getattr(spec, "category", "general"), []).append(spec)
    for cat in sorted(by_cat):
        out.print(f"[{cat}]", token="bold")
        for spec in sorted(by_cat[cat], key=lambda s: s.name):
            out.print(f"  {spec.name:<14} {spec.help}")
        out.rule()


def _completion(argv: list[str]) -> int:
    """Handler for `hive completion <shell>`. Prints installable script."""
    if not argv or argv[0] not in ("bash", "zsh", "fish"):
        sys.stderr.write("usage: hive completion <bash|zsh|fish>\n")
        sys.stderr.write("error: unknown shell\n")
        return 2
    from .completion import CompletionSpec, bash_completion, fish_completion, zsh_completion
    specs = [
        CompletionSpec(
            name=s.name, category=getattr(s, "category", "general"),
            help=s.help, subcommands=tuple(s.subcommands.keys()) if s.subcommands else (),
        )
        for s in _registry_mod.REGISTRY.values()
    ]
    if argv[0] == "bash":
        print(bash_completion(specs), end="")
    elif argv[0] == "zsh":
        print(zsh_completion(specs), end="")
    else:
        print(fish_completion(specs), end="")
    return 0


def _populate_registry() -> None:
    _registry_mod.REGISTRY["chat"] = _registry_mod.CommandSpec(
        name="chat",
        help="interactive REPL (default)",
        handler_name="_chat",
        category="core",
    )
    _registry_mod.REGISTRY["ask"] = _registry_mod.CommandSpec(
        name="ask",
        help="one-shot turn",
        handler_name="_ask",
        args=(("MSG", str, "message"),),
        category="core",
    )
    _registry_mod.REGISTRY["serve"] = _registry_mod.CommandSpec(
        name="serve",
        help="run the FastAPI gateway",
        handler_name="_serve",
        category="runtime",
    )
    _registry_mod.REGISTRY["init"] = _registry_mod.CommandSpec(
        name="init",
        help="first-time setup wizard",
        handler_name="_init",
        category="runtime",
    )
    _registry_mod.REGISTRY["doctor"] = _registry_mod.CommandSpec(
        name="doctor",
        help="environment health checks",
        handler_name="",  # dispatched inline by main
        args=(("--fix", None, "auto-repair common issues"),),
        category="runtime",
    )
    _registry_mod.REGISTRY["mcp-serve"] = _registry_mod.CommandSpec(
        name="mcp-serve",
        help="serve Hive's tool registry as an MCP stdio server",
        handler_name="_mcp_serve",
        category="runtime",
    )
    _registry_mod.REGISTRY["heartbeat"] = _registry_mod.CommandSpec(
        name="heartbeat",
        help="run the autonomy heartbeat once",
        handler_name="_heartbeat",
        category="runtime",
    )
    _registry_mod.REGISTRY["consolidate"] = _registry_mod.CommandSpec(
        name="consolidate",
        help="consolidate short-term memory into long-term",
        handler_name="_consolidate",
        category="runtime",
    )
    _registry_mod.REGISTRY["version"] = _registry_mod.CommandSpec(
        name="version",
        help="print version and config summary",
        handler_name="_version",
        category="core",
    )
    _registry_mod.REGISTRY["status"] = _registry_mod.CommandSpec(
        name="status",
        help="config + environment health summary",
        handler_name="_status",
        category="ops",
    )
    _registry_mod.REGISTRY["logs"] = _registry_mod.CommandSpec(
        name="logs",
        help="recent audit log entries",
        handler_name="_logs",
        args=(("--tail", _int_or(20), "lines to show"),),
        category="ops",
    )
    _registry_mod.REGISTRY["budget"] = _registry_mod.CommandSpec(
        name="budget",
        help="budget forecast + warning status",
        handler_name="_budget",
        category="gateway",
    )
    _registry_mod.REGISTRY["approvals"] = _registry_mod.CommandSpec(
        name="approvals",
        help="pending approval queue",
        handler_name="_approvals",
        category="gateway",
    )
    _registry_mod.REGISTRY["selfmod-history"] = _registry_mod.CommandSpec(
        name="selfmod-history",
        help="durable self-mod proposal history",
        handler_name="_selfmod_history",
        args=(("--limit", _int_or(20), "max records to show"),),
        category="ops",
    )
    _registry_mod.REGISTRY["learning"] = _registry_mod.CommandSpec(
        name="learning",
        help="learning loop introspection (SPRINT_6 P-F)",
        handler_name="_learning_dispatch",
        category="runtime",
        subcommands={
            "status": _registry_mod.CommandSpec(
                name="status",
                help="show aggregate + recent loop outcomes",
                handler_name="_learning_status",
                args=(("--limit", _int_or(10), "max recent loops"),),
                category="ops",
            ),
            "replay": _registry_mod.CommandSpec(
                name="replay",
                help="replay a recorded loop",
                handler_name="_learning_replay",
                args=(("ID", str, "loop id"),),
                category="ops",
            ),
        },
    )
    _registry_mod.REGISTRY["completion"] = _registry_mod.CommandSpec(
        name="completion",
        help="emit shell completion script (bash|zsh|fish)",
        handler_name="_completion",
        category="core",
        args=(("SHELL", str, "bash|zsh|fish"),),
    )


_populate_registry()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_USAGE = "usage: hive [chat|init|ask|serve|heartbeat|consolidate|doctor|mcp-serve|version|status|logs|budget|approvals|selfmod-history|learning|completion]"


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)

    if not args_list:
        return _run_async(_chat())
    if args_list[0] in ("-h", "--help", "help"):
        _build_help_overview()
        return 0

    cmd = args_list[0]
    if cmd not in _registry_mod.REGISTRY:
        print(f"unknown command: {cmd}\n{_USAGE}", file=sys.stderr)
        return 2

    if cmd == "doctor":
        from hive.core import doctor
        fix = "--fix" in args_list
        return 0 if doctor.run(fix=fix) else 1

    if cmd == "ask":
        msg = " ".join(args_list[1:]).strip()
        if not msg:
            print("usage: hive ask \"<message>\"", file=sys.stderr)
            return 2
        return _run_async(_ask(msg))

    try:
        spec, parsed = _parser_mod.parse(args_list)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        # Learning subcommand errors return 1 (consistent with original _learning_dispatch)
        if cmd == "learning" and code == 2:
            return 1
        return code

    if cmd == "completion":
        # `hive completion <bash|zsh|fish>` — argv[0] is the shell name.
        return _completion(args_list[1:])

    if cmd == "logs":
        tail = getattr(parsed, "tail", 20)
        try:
            tail = int(tail)
        except (ValueError, TypeError):
            tail = 20
        return _logs(tail)
    if cmd == "selfmod-history":
        limit = getattr(parsed, "limit", 20)
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            limit = 20
        return _run_async(_selfmod_history(limit=limit))
    if cmd == "learning" and spec.name == "status":
        limit = getattr(parsed, "limit", "10")
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            limit = 10
        return _learning_status(limit=limit)
    if cmd == "learning" and spec.name == "replay":
        raw = getattr(parsed, "ID", None)
        try:
            return _learning_replay(int(raw))
        except (ValueError, TypeError):
            print(_yellow(f"\n  Invalid loop_id: {raw}"))
            return 1
    if cmd == "learning" and getattr(parsed, "subcommand", None) is None:
        # `hive learning` with no sub → preserve original _learning_dispatch behavior
        return _learning_dispatch(args_list[1:])

    handler = globals().get(spec.handler_name) if spec.handler_name else None
    if handler is None:
        print(f"unknown command: {cmd}\n{_USAGE}", file=sys.stderr)
        return 2
    if asyncio.iscoroutinefunction(handler):
        return _run_async(handler())
    return handler()


if __name__ == "__main__":
    raise SystemExit(main())
