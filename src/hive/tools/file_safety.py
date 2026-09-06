"""
file_safety.py — sensitive-path denylist for tool execution.

Ported from Hermes agent/file_safety.py (HERMES_REFERENCE REUSE-READY #13,
Direct/Zero). The ToolExecutor checks candidate file paths against this list
before dispatching write/execute tool calls so the agent cannot overwrite
credentials, shell config, or the PROTECTED HiveOS files.
"""
from __future__ import annotations

import os
import posixpath
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_REPO_SENSITIVE_RELATIVE = frozenset({
    "config/soul.md",
    "core/approval_gate.py",
    ".git/config",
    "pyproject.toml",
    ".github/workflows/ci.yml",
})


def _real(p: str) -> str:
    try:
        return os.path.realpath(p)
    except Exception:  # noqa: BLE001
        return p


def _path_key(path: str | os.PathLike[str]) -> str:
    """Normalize a path for case-insensitive, slash-independent comparison."""
    try:
        raw = os.fspath(path)
    except TypeError:
        raw = str(path)
    return posixpath.normpath(raw.replace("\\", "/")).casefold()


def _real_key(path: str | os.PathLike[str]) -> str:
    return _path_key(_real(os.fspath(path)))


def _repo_sensitive(path: str | os.PathLike[str]) -> bool:
    raw_key = _path_key(path).lstrip("./")
    if raw_key in _REPO_SENSITIVE_RELATIVE:
        return True
    repo_key = _path_key(REPO_ROOT)
    return _real_key(path) in {
        f"{repo_key}/{relative}" for relative in _REPO_SENSITIVE_RELATIVE
    }


def _add_path_aliases(result: set[str], path: str | os.PathLike[str]) -> None:
    real = _real(os.fspath(path))
    result.add(real)
    result.add(_path_key(real))


def build_denied_write_paths(home: str | None = None) -> frozenset[str]:
    """Return absolute real paths that must never be written by a tool call."""
    h = home or os.path.expanduser("~")
    paths = {
        # SSH
        os.path.join(h, ".ssh", "authorized_keys"),
        os.path.join(h, ".ssh", "id_rsa"),
        os.path.join(h, ".ssh", "id_ed25519"),
        os.path.join(h, ".ssh", "config"),
        # Shell init
        os.path.join(h, ".bashrc"),
        os.path.join(h, ".zshrc"),
        os.path.join(h, ".profile"),
        os.path.join(h, ".bash_profile"),
        os.path.join(h, ".zprofile"),
        # Credential files
        os.path.join(h, ".netrc"),
        os.path.join(h, ".pgpass"),
        os.path.join(h, ".npmrc"),
        os.path.join(h, ".pypirc"),
        os.path.join(h, ".git-credentials"),
        # System
        "/etc/sudoers",
        "/etc/passwd",
        "/etc/shadow",
        "/etc/hosts",
        # HiveOS PROTECTED files (extra guard in addition to self_mod checks)
        REPO_ROOT / "Config" / "SOUL.md",
        REPO_ROOT / "Core" / "approval_gate.py",
        REPO_ROOT / ".git" / "config",
        REPO_ROOT / "pyproject.toml",
        REPO_ROOT / ".github" / "workflows" / "ci.yml",
    }
    result: set[str] = set()
    for path in paths:
        _add_path_aliases(result, path)
    return frozenset(result)


# Module-level singleton built once; tools executor imports this.
DENIED_WRITE_PATHS: frozenset[str] = build_denied_write_paths()
_DENIED_WRITE_PATH_KEYS = frozenset(_path_key(path) for path in DENIED_WRITE_PATHS)


def is_write_denied(path: str) -> bool:
    """True if writing to `path` is forbidden."""
    return _repo_sensitive(path) or _real_key(path) in _DENIED_WRITE_PATH_KEYS


def has_traversal(path: str) -> bool:
    """True if the path contains directory traversal sequences (..)."""
    p = Path(path)
    return ".." in p.parts


def has_unsafe_symlink(path: str) -> bool:
    """True if `path` or any parent component is a symlink outside the repo root."""
    try:
        check = Path(path)
        while check != check.parent:
            if check.is_symlink():
                target = check.resolve()
                # Allow symlinks that stay inside the repo root; block escapes.
                repo_key = _path_key(REPO_ROOT)
                target_key = _path_key(target)
                if target_key != repo_key and not target_key.startswith(repo_key + "/"):
                    return True
            check = check.parent
        return False
    except Exception:  # noqa: BLE001
        return False


def check_path(path: str, *, operation: str = "write") -> str | None:
    """Return an error string if `path` is off-limits, else None."""
    if operation == "read" and _repo_sensitive(path):
        return f"reading {path!r} is not permitted (sensitive repository path)"
    if operation in ("write", "delete", "move"):
        if has_traversal(path):
            return f"path traversal not permitted: {path!r}"
        if is_write_denied(path):
            return f"writing to {path!r} is not permitted (sensitive path)"
        if has_unsafe_symlink(path):
            return f"writing through symlink escape is not permitted: {path!r}"
    return None
