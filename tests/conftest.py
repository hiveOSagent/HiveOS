"""Shared pytest fixtures for the HiveOS test suite."""
from __future__ import annotations

import os

import keyring
import pytest
from keyring.backend import KeyringBackend

from hive.core.approval import gate as _approval_gate
from hive.core.approval_enhancements import enhance as _approval_enhance
from hive.core.redact import clear_registered_secret_values
import hive.core.config as _config_mod

# Env vars injected by dotenv that tests must not see (tests use their own tmp
# roots and hardcode the default "change_me" secret).  We snapshot and restore
# these so that the first test that calls get_config() doesn't pollute the rest.
_DOTENV_VARS = ("HIVE_SECRET", "HIVE_PRODUCTION", "HIVE_HOST", "HIVE_PORT", "HIVE_DATA_DIR", "HIVE_STATE_DB",
                "MNEMOSYNE_HOME", "OBSIDIAN_VAULT_PATH", "MINIMAX_API_KEY", "HIVE_GITHUB_TOKEN",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "HIVE_APPROVER_KEY",
                "HIVE_TELEGRAM_ALLOWED_USER_IDS", "HIVE_TELEGRAM_ALLOWED_CHAT_IDS",
                "HIVE_SLACK_ALLOWED_USER_IDS", "HIVE_DISCORD_ALLOWED_USER_IDS",
                "HIVE_EMAIL_ALLOWED_SENDERS")


class _TestKeyring(KeyringBackend):
    """In-memory OS-keyring seam for deterministic credential-store tests."""

    priority = 1

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._values.pop((service, username), None)


_TestKeyring.__module__ = "keyring.backends.Windows"


@pytest.fixture(autouse=True)
def _reset_globals():
    """Reset module-level singletons before and after every test to prevent
    state leakage between tests:

    - approval gate _pending and the enhancement kill switch: tests enqueue
      approvals or exercise emergency stop; their process-global state must not
      alter later tool/spec-search tests.
    - _CONFIG: HiveOS.build() calls set_config(cfg) which mutates the
      module-level global; without a reset a test that calls get_config()
      without building first may see another test's config.
    - os.environ dotenv vars: load_dotenv() called by get_config() sets
      HIVE_SECRET etc. into os.environ for the process lifetime; tests that
      use _config(tmp_path, load_dotenv=False) with hardcoded "change_me"
      would get 401s if HIVE_SECRET was already set from a prior test.
    """
    saved_config = _config_mod._CONFIG
    saved_env = {k: os.environ.get(k) for k in _DOTENV_VARS}
    saved_keyring = keyring.get_keyring()
    keyring.set_keyring(_TestKeyring())
    clear_registered_secret_values()
    _approval_gate._pending.clear()
    _approval_enhance.configure_persistence(None)
    _approval_enhance.release_kill_switch(released_by="pytest fixture")
    _config_mod._CONFIG = None   # start each test from a clean config slate
    # Remove dotenv-loaded vars so tests see only defaults
    for k in _DOTENV_VARS:
        os.environ.pop(k, None)
    yield
    _approval_gate._pending.clear()
    _approval_enhance.configure_persistence(None)
    _approval_enhance.release_kill_switch(released_by="pytest fixture")
    _config_mod._CONFIG = saved_config
    # Restore pre-test env state
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    keyring.set_keyring(saved_keyring)
    clear_registered_secret_values()
