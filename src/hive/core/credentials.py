"""Credential storage backed by the operating system's secret service.

Only a 0o600 manifest of credential names is kept under Hive's data directory.
Values live in the platform keyring, so a readable project/data directory no
longer exposes credentials.  Existing plaintext ``credentials.json`` stores are
migrated atomically on their first successful access; if no safe keyring backend
is available, the migration fails closed and leaves the legacy file untouched.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from hive.core import config
from hive.core.redact import register_secret_values

log = logging.getLogger("hive.credentials")
_MANIFEST_VERSION = 2
_SERVICE_PREFIX = "HiveOS.credentials"


class CredentialStoreError(RuntimeError):
    """Raised when the OS credential backend cannot safely store a secret."""


def _path() -> Path:
    # Named product artifact (the secret vault), not runtime state — the
    # SQLite-first rule targets queues/indexes/cursors (ARCHITECTURE_REVIEW §F4).
    return config.get_config().data_dir / "credentials.json"


def _service_name() -> str:
    """Scope secrets to this HiveOS root without revealing the local path."""
    root = str(config.get_config().root.resolve()).encode("utf-8")
    return f"{_SERVICE_PREFIX}.{hashlib.sha256(root).hexdigest()[:16]}"


def _keyring() -> Any:
    """Return the configured OS keyring or fail closed with a useful error."""
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise CredentialStoreError("keyring dependency is unavailable") from exc
    backend = keyring.get_keyring()
    backend_module = type(backend).__module__.casefold()
    backend_name = type(backend).__name__.casefold()
    if (
        backend_module.startswith(("keyring.backends.fail", "keyrings.alt"))
        or "plaintext" in backend_name
    ):
        raise CredentialStoreError(
            "no secure OS credential backend is configured; refusing plaintext credential storage"
        )
    return keyring


def _read_raw() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("credential manifest read failed: %s", exc)
        return {}
    return value if isinstance(value, dict) else {}


def _manifest_keys(raw: dict[str, Any]) -> list[str] | None:
    if raw.get("version") != _MANIFEST_VERSION:
        return None
    keys = raw.get("keys")
    if not isinstance(keys, list) or any(not isinstance(key, str) or not key for key in keys):
        log.warning("credential manifest has an invalid key list")
        return []
    return list(dict.fromkeys(keys))


def _legacy_values(raw: dict[str, Any]) -> dict[str, str]:
    """Return a valid legacy plaintext payload, excluding manifest-like data."""
    if not raw or "version" in raw or any(not isinstance(key, str) or not isinstance(value, str)
                                           for key, value in raw.items()):
        return {}
    return dict(raw)


def _write_manifest(keys: list[str]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"version": _MANIFEST_VERSION, "keys": sorted(set(keys))}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:  # pragma: no cover - non-POSIX
        pass
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - non-POSIX
        pass


def _migrate_legacy(values: dict[str, str]) -> dict[str, str]:
    """Move legacy plaintext values to the OS keyring before replacing the file."""
    keyring = _keyring()
    try:
        for key, value in values.items():
            keyring.set_password(_service_name(), key, value)
    except Exception as exc:  # noqa: BLE001 - backend exceptions vary by OS
        raise CredentialStoreError("credential migration to the OS keyring failed") from exc
    _write_manifest(list(values))
    return values


def _load() -> dict[str, str]:
    """Load values from keyring, migrating an old plaintext vault if necessary."""
    raw = _read_raw()
    keys = _manifest_keys(raw)
    if keys is None:
        legacy = _legacy_values(raw)
        values = _migrate_legacy(legacy) if legacy else {}
    else:
        keyring = _keyring()
        values = {}
        try:
            for key in keys:
                value = keyring.get_password(_service_name(), key)
                if value is not None:
                    values[key] = value
        except Exception as exc:  # noqa: BLE001 - backend exceptions vary by OS
            raise CredentialStoreError("credential read from the OS keyring failed") from exc
    register_secret_values(values.values())
    return values


def save(key: str, value: str) -> None:
    if not isinstance(key, str) or not key:
        raise ValueError("credential key must be a non-empty string")
    if not isinstance(value, str):
        raise TypeError("credential value must be a string")
    # Load first so an old plaintext file is migrated before its manifest is
    # updated.  This preserves every existing credential during an overwrite.
    _load()
    existing_keys = _manifest_keys(_read_raw()) or []
    keyring = _keyring()
    try:
        keyring.set_password(_service_name(), key, value)
    except Exception as exc:  # noqa: BLE001 - backend exceptions vary by OS
        raise CredentialStoreError("credential write to the OS keyring failed") from exc
    register_secret_values((value,))
    _write_manifest(existing_keys + [key])


def delete(key: str) -> bool:
    """Delete one stored credential and remove its name from the local manifest."""
    keys = _manifest_keys(_read_raw()) or []
    if key not in keys:
        return False
    keyring = _keyring()
    try:
        keyring.delete_password(_service_name(), key)
    except Exception as exc:  # noqa: BLE001 - backend exceptions vary by OS
        raise CredentialStoreError("credential deletion from the OS keyring failed") from exc
    _write_manifest([existing for existing in keys if existing != key])
    return True


def get(key: str, default: str | None = None) -> str | None:
    return _load().get(key, os.getenv(key, default))


def inject() -> int:
    """Load stored credentials into os.environ (does not overwrite existing)."""
    n = 0
    for k, v in _load().items():
        if k not in os.environ:
            os.environ[k] = v
            n += 1
    return n
