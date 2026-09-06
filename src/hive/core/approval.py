"""
approval.py — bridge to the PROTECTED approval gate, in place.

Core/approval_gate.py is the canonical danger firewall and is NEVER moved or
edited (see SOUL.md + docs/references/SYNTHESIS.md Part A.4). The new package must
not duplicate it; instead it loads the existing file by path and re-exports its
symbols. The canonical logic stays the untouched Core/approval_gate.py.
"""
from __future__ import annotations

import importlib.util
import os
import posixpath
import re
import shlex
from pathlib import Path

# src/hive/core/approval.py -> parents[3] == repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
_GATE_PATH = REPO_ROOT / "Core" / "approval_gate.py"

_spec = importlib.util.spec_from_file_location("hive._protected_approval_gate", _GATE_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover - file must exist
    raise ImportError(f"cannot load protected approval gate at {_GATE_PATH}")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# Re-export the canonical symbols (defined in the untouched protected file).
gate = _mod.gate
PROTECTED_PATHS = _mod.PROTECTED_PATHS
DANGEROUS_TOOLS = _mod.DANGEROUS_TOOLS


# The canonical gate remains the immutable source of truth. This bridge adds
# containment checks that the legacy gate cannot safely grow without changing
# its protected file. Shell is deliberately allowlisted: a command that is
# not proven to be a harmless, read-only inspection is approval-bound.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROTECTED_RELATIVE_PATHS = frozenset(
    posixpath.normpath(str(path).replace("\\", "/")).casefold()
    for path in PROTECTED_PATHS
)
_SAFE_SHELL_COMMANDS = frozenset({
    "cd", "date", "dir", "echo", "hostname", "ls", "printf", "pwd",
    "type", "ver", "where", "which", "whoami",
})
_SAFE_GIT_SUBCOMMANDS = frozenset({"branch", "describe", "diff", "log", "show", "status"})
_SHELL_META = re.compile(r"[\r\n;&|<>\x60$()]|\u0000")
_PATH_ARGUMENTS = ("path", "file", "filename", "destination")


def _normalise_path(value: object) -> str:
    """Return a case-insensitive, separator-independent normalized path."""
    try:
        raw = os.fspath(value)
    except TypeError:
        raw = str(value)
    return posixpath.normpath(raw.replace("\\", "/")).casefold()


def _is_protected_path(value: object) -> bool:
    normalized = _normalise_path(value)
    repo = _normalise_path(_REPO_ROOT)
    for relative in _PROTECTED_RELATIVE_PATHS:
        if normalized == relative or normalized.endswith("/" + relative):
            return True
        if normalized == f"{repo}/{relative}":
            return True
    return False


def _args_touch_protected_path(args: dict) -> bool:
    return any(_is_protected_path(args.get(name, "")) for name in _PATH_ARGUMENTS)


def _is_safe_shell_command(command: object) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    if _SHELL_META.search(command):
        return False
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        return False
    if not tokens:
        return False

    executable = tokens[0].strip('"\'').rsplit("/", 1)[-1].rsplit("\\", 1)[-1].casefold()
    if executable in _SAFE_SHELL_COMMANDS:
        return True
    if executable == "git":
        # Only read-only inspection subcommands are allowlisted. In
        # particular, git branch -D must not inherit the branch allowance.
        subcommand = next((token.casefold() for token in tokens[1:]
                           if not token.startswith("-")), "")
        if subcommand not in _SAFE_GIT_SUBCOMMANDS:
            return False
        mutating_flags = {"-d", "-D", "-m", "-M", "-c", "--delete", "--move",
                          "--copy", "--force", "-f", "--output"}
        return not any(token.casefold() in mutating_flags for token in tokens[1:])
    if executable in {"python", "python3", "py"}:
        return all(token.casefold() in {"-v", "-vv", "-version", "--version"}
                   for token in tokens[1:])
    return False


class ApprovalGate(_mod.ApprovalGate):
    """Canonical gate API with the bridge's fail-closed classification."""

    def is_dangerous(self, tool: str, args: dict) -> bool:
        if tool == "shell" and not _is_safe_shell_command(args.get("cmd")):
            return True
        if _args_touch_protected_path(args):
            return True
        return bool(super().is_dangerous(tool, args))


class _ContainmentGate:
    """Proxy the immutable gate while adding fail-closed boundary checks."""

    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    def is_dangerous(self, tool: str, args: dict) -> bool:
        if tool == "shell":
            command = args.get("cmd")
            if not _is_safe_shell_command(command):
                return True
        if _args_touch_protected_path(args):
            return True
        return bool(self._delegate.is_dangerous(tool, args))

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


# Keep the canonical object intact for the enhancements layer and all queue
# operations, while exposing the hardened classification to callers.
gate = _ContainmentGate(_mod.gate)

__all__ = ["gate", "ApprovalGate", "PROTECTED_PATHS", "DANGEROUS_TOOLS"]
