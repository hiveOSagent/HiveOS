"""
config.py — canonical HiveOS configuration as a typed, immutable object.

Built explicitly (no import-time side effects): importing this module reads no
env, writes no files, creates no dirs. `HiveConfig.from_env()` snapshots the
environment into a frozen dataclass; HiveOS.build() (P7) builds it once and
injects it. Tests construct alternate configs; `core.doctor` diffs/migrates shapes
against it (OpenClaw rule: runtime reads only the canonical shape —
docs/references/OPENCLAW_REFERENCE.md §2; rationale in ARCHITECTURE_REVIEW §F3).

Model strings/endpoints stay env-driven (MiniMax moves M2 -> M3). The PROTECTED
SOUL.md is referenced in place via core.soul (never relocated until P9).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from hive.core.soul import REPO_ROOT  # SOUL is loaded lazily by callers, not at config import


def _maybe_load_dotenv(root: Path) -> None:
    """Best-effort .env load; explicit, never at import."""
    try:
        from dotenv import load_dotenv

        load_dotenv(root / ".env")
    except Exception:  # noqa: BLE001 - dotenv optional
        pass


def _parse_csv_env(name: str) -> frozenset[str]:
    """Return non-empty, trimmed values from a comma-separated environment variable."""
    return frozenset(value.strip() for value in os.getenv(name, "").split(",") if value.strip())


@dataclass(frozen=True, slots=True)
class HiveConfig:
    root: Path
    data_dir: Path
    state_db: Path
    # Executor provider selection (minimax | anthropic)
    exec_provider: str
    # MiniMax (primary executor)
    minimax_anthropic_base: str
    minimax_openai_base: str
    minimax_api_key: str
    # Anthropic (alternative executor)
    anthropic_base: str
    anthropic_api_key: str
    exec_model: str
    exec_fallback_model: str
    aux_model: str
    # ChatGPT Plus planner (thinking only, via Codex OAuth)
    planner_cmd: str
    planner_enabled: bool
    planner_timeout: float
    # Budgeter
    remains_url: str
    daily_call_cap: int
    window_warn_pct: float
    # Gateway
    host: str
    port: int
    secret: str
    production_mode: bool
    # Memory
    mnemosyne_mcp_url: str
    mnemosyne_home: Path  # local SQLite path for the Mnemosyne provider
    obsidian_vault: Path
    # Autonomy
    heartbeat_sec: int
    max_concurrent_agents: int
    # Hive's own GitHub identity
    github_token: str
    github_repo: str
    github_owner: str
    # Telegram surface (optional)
    telegram_token: str
    telegram_webhook_secret: str
    telegram_allowed_user_ids: frozenset[str]
    telegram_allowed_chat_ids: frozenset[str]
    # Self-mod sandbox: optional for supervised changes, mandatory for autonomous self-mod.
    sandbox_image: str
    # MCP stdio servers to load at startup: ';'-separated command lines (A2)
    mcp_servers: tuple[str, ...]
    # Agent loop limits (configurable so Hive can handle long development tasks)
    max_iterations: int   # LLM-tool turns per conversation turn (HIVE_MAX_ITERATIONS)
    max_per_tool: int     # per-tool call budget within one turn (HIVE_MAX_PER_TOOL)
    # Self-improvement: min recent failures before triggering self-diagnose (HIVE_SELFMOD_THRESHOLD)
    selfmod_failure_threshold: int
    # Learning loop (SPRINT_6 P-F): opt-in gate for trace→evolve→eval self-improvement
    learning_loop_enabled: bool
    learning_eval_timeout: float  # seconds (HIVE_LEARNING_EVAL_TIMEOUT)
    # Tool executor: max seconds per tool call before it is killed (HIVE_TOOL_TIMEOUT, 0=no limit)
    tool_timeout: float
    # Shell provider: "local" (default) or "docker" for container isolation
    shell_provider: str
    shell_docker_image: str
    # Gateway security
    cors_origins: str          # HIVE_CORS_ORIGINS: comma-sep origins or "*"
    max_message_len: int       # HIVE_MAX_MESSAGE_LEN: max chars in a message
    ws_idle_timeout: float     # HIVE_WS_IDLE_TIMEOUT: WebSocket idle timeout seconds
    # Multi-channel messaging (ExternalMessage tool)
    smtp_host: str             # HIVE_SMTP_HOST: SMTP server hostname (e.g. smtp.gmail.com)
    smtp_port: int             # HIVE_SMTP_PORT: SMTP port (default 587 for STARTTLS)
    smtp_user: str             # HIVE_SMTP_USER: SMTP login / From address
    smtp_pass: str             # HIVE_SMTP_PASS: SMTP password or app-password
    smtp_to: str               # HIVE_SMTP_TO: recipient address for Hive emails
    slack_webhook: str         # HIVE_SLACK_WEBHOOK: Slack incoming webhook URL
    discord_webhook: str       # HIVE_DISCORD_WEBHOOK: Discord incoming webhook URL
    # Inbound channel credentials (SPRINT_6 P-E)
    slack_bot_token: str          # HIVE_SLACK_BOT_TOKEN: bot token for chat.postMessage
    slack_signing_secret: str     # HIVE_SLACK_SIGNING_SECRET: HMAC-SHA256 secret for webhook
    discord_bot_token: str        # HIVE_DISCORD_BOT_TOKEN: bot token for /channels/{id}/messages
    discord_public_key: str       # HIVE_DISCORD_PUBLIC_KEY: Ed25519 public key for webhook
    discord_application_id: str   # HIVE_DISCORD_APP_ID: application id for webhook URL
    smtp_from: str                # HIVE_SMTP_FROM: From address used when sending replies
    smtp_webhook_secret: str      # HIVE_SMTP_WEBHOOK_SECRET: shared secret for /email/webhook
    # Inbound sender allowlists.  An enabled surface with an empty relevant
    # allowlist is rejected at startup rather than accepting every sender.
    slack_allowed_user_ids: frozenset[str]
    discord_allowed_user_ids: frozenset[str]
    email_allowed_senders: frozenset[str]
    # Self-mod proactive: run self_diagnose every N heartbeat ticks (0 = disabled)
    selfmod_proactive_interval: int  # HIVE_SELFMOD_PROACTIVE_INTERVAL
    # Self-mod safety pre-flight checks (Pillar 4): run static checks before opening
    # a draft PR / requesting approval. Default ON; disable only for benchmarking.
    selfmod_enable_safety_checks: bool  # HIVE_SELFMOD_ENABLE_SAFETY_CHECKS
    # Self-mod safety: max files an AUTO-tier edit may touch before escalating to REVIEW.
    selfmod_safety_max_files: int  # HIVE_SELFMOD_SAFETY_MAX_FILES
    # Self-mod failure-trigger cooldown: min seconds between auto self-mod attempts
    # (prevents the LLM diagnoser from running on every tick when failures persist).
    selfmod_failure_cooldown_sec: float  # HIVE_SELFMOD_FAILURE_COOLDOWN_SEC
    # Proactive heartbeat scan: every N seconds (0 disables).
    heartbeat_proactive_interval_sec: int  # HIVE_HEARTBEAT_PROACTIVE_INTERVAL_SEC
    # Stale-fact threshold (days).
    heartbeat_stale_fact_days: int  # HIVE_HEARTBEAT_STALE_FACT_DAYS
    # Stale-commitment threshold (days).
    heartbeat_stale_commitment_days: int  # HIVE_HEARTBEAT_STALE_COMMITMENT_DAYS
    # Deploy targets: SSH and Docker (optional)
    deploy_ssh_host: str   # HIVE_DEPLOY_SSH_HOST: user@host for SSH deploys
    deploy_ssh_key: str    # HIVE_DEPLOY_SSH_KEY: path to private key file (empty = default key)
    # Memory entity resolution (SPRINT_7 Batch D): group facts by canonical key
    entity_resolution_enabled: bool   # HIVE_ENTITY_RESOLUTION_ENABLED (default True)
    entity_resolution_alias_map: str  # HIVE_ENTITY_RESOLUTION_ALIAS_MAP: inline JSON ({...}) or ''
    # Stripe payment backend (optional)
    stripe_secret_key: str   # STRIPE_SECRET_KEY: Stripe secret key (sk_live_... or sk_test_...)
    stripe_customer_id: str  # STRIPE_CUSTOMER_ID: default Stripe customer ID to charge
    # Budget forecast alert (SPRINT_7 Batch F): days_until_cap threshold for sending
    # the Telegram budget alert (default 1 = alert when cap is hit within a day).
    budget_forecast_alert_days: int  # HIVE_BUDGET_FORECAST_ALERT_DAYS
    # Optional USD cap used only by spend projections and alerts (0 disables it).
    budget_daily_spend_cap_usd: float  # HIVE_DAILY_SPEND_CAP_USD
    # P0 autonomy gates: both stay opt-in until durable task and approval recovery exist.
    autonomy_enabled: bool = False
    autonomous_selfmod_enabled: bool = False
    # Out-of-band approval credential (HIVE_APPROVER_KEY).  Kept at the end with
    # a default so direct test/config construction remains backward-compatible.
    approver_key: str = ""

    @classmethod
    def from_env(cls, root: Path | str | None = None, *, load_dotenv: bool = True) -> "HiveConfig":
        root = Path(root) if root else REPO_ROOT   # coerce: callers may pass a str path
        if load_dotenv:
            _maybe_load_dotenv(root)
        data_dir = Path(os.getenv("HIVE_DATA_DIR", str(root / "data")))
        return cls(
            root=root,
            data_dir=data_dir,
            state_db=Path(os.getenv("HIVE_STATE_DB", str(data_dir / "hive.sqlite"))),
            exec_provider=os.getenv("HIVE_EXEC_PROVIDER", "minimax"),
            minimax_anthropic_base=os.getenv("MINIMAX_ANTHROPIC_BASE", "https://api.minimax.io/anthropic"),
            minimax_openai_base=os.getenv("MINIMAX_OPENAI_BASE", "https://api.minimax.io/v1"),
            minimax_api_key=os.getenv("MINIMAX_API_KEY", ""),
            anthropic_base=os.getenv("ANTHROPIC_BASE", "https://api.anthropic.com"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            exec_model=os.getenv("HIVE_EXEC_MODEL", "MiniMax-M3"),
            exec_fallback_model=os.getenv("HIVE_EXEC_FALLBACK_MODEL", "MiniMax-M2.7"),
            aux_model=os.getenv("HIVE_AUX_MODEL", "MiniMax-M2.7"),
            planner_cmd=os.getenv("HIVE_PLANNER_CMD", "codex exec"),
            planner_enabled=os.getenv("HIVE_PLANNER_ENABLED", "false").lower() == "true",
            planner_timeout=float(os.getenv("HIVE_PLANNER_TIMEOUT", "120")),
            remains_url=os.getenv("HIVE_REMAINS_URL", "https://api.minimax.io/v1/token_plan/remains"),
            daily_call_cap=int(os.getenv("HIVE_DAILY_CALL_CAP", "3000")),
            window_warn_pct=float(os.getenv("HIVE_WINDOW_WARN_PCT", "70")),
            host=os.getenv("HIVE_HOST", "0.0.0.0"),
            port=int(os.getenv("HIVE_PORT", "8088")),
            secret=os.getenv("HIVE_SECRET", "change_me"),
            production_mode=os.getenv("HIVE_PRODUCTION", "false").lower() == "true",
            mnemosyne_mcp_url=os.getenv("MNEMOSYNE_MCP_URL", ""),
            mnemosyne_home=Path(os.getenv("MNEMOSYNE_HOME", str(data_dir / "mnemosyne"))),
            obsidian_vault=Path(os.getenv("OBSIDIAN_VAULT_PATH", str(root / "vault"))),
            heartbeat_sec=int(os.getenv("HIVE_HEARTBEAT_SEC", "900")),
            max_concurrent_agents=int(os.getenv("HIVE_MAX_AGENTS", "3")),
            autonomy_enabled=os.getenv("HIVE_AUTONOMY_ENABLED", "false").lower() == "true",
            autonomous_selfmod_enabled=os.getenv("HIVE_AUTONOMOUS_SELFMOD_ENABLED", "false").lower() == "true",
            github_token=os.getenv("HIVE_GITHUB_TOKEN", ""),
            github_repo=os.getenv("HIVE_GITHUB_REPO", ""),
            github_owner=os.getenv("HIVE_GITHUB_OWNER", ""),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET", ""),
            telegram_allowed_user_ids=_parse_csv_env("HIVE_TELEGRAM_ALLOWED_USER_IDS"),
            telegram_allowed_chat_ids=_parse_csv_env("HIVE_TELEGRAM_ALLOWED_CHAT_IDS"),
            sandbox_image=os.getenv("HIVE_SANDBOX_IMAGE", ""),
            mcp_servers=tuple(s.strip() for s in os.getenv("HIVE_MCP_SERVERS", "").split(";")
                              if s.strip()),
            max_iterations=int(os.getenv("HIVE_MAX_ITERATIONS", "30")),
            max_per_tool=int(os.getenv("HIVE_MAX_PER_TOOL", "50")),
            selfmod_failure_threshold=int(os.getenv("HIVE_SELFMOD_THRESHOLD", "3")),
            learning_loop_enabled=os.getenv("HIVE_LEARNING_LOOP_ENABLED", "false").lower() == "true",
            learning_eval_timeout=float(os.getenv("HIVE_LEARNING_EVAL_TIMEOUT", "60")),
            tool_timeout=float(os.getenv("HIVE_TOOL_TIMEOUT", "60")),
            shell_provider=os.getenv("HIVE_SHELL_PROVIDER", "local"),
            shell_docker_image=os.getenv("HIVE_SHELL_DOCKER_IMAGE", "alpine:latest"),
            cors_origins=os.getenv("HIVE_CORS_ORIGINS", "*"),
            max_message_len=int(os.getenv("HIVE_MAX_MESSAGE_LEN", "32000")),
            ws_idle_timeout=float(os.getenv("HIVE_WS_IDLE_TIMEOUT", "300")),
            smtp_host=os.getenv("HIVE_SMTP_HOST", ""),
            smtp_port=int(os.getenv("HIVE_SMTP_PORT", "587")),
            smtp_user=os.getenv("HIVE_SMTP_USER", ""),
            smtp_pass=os.getenv("HIVE_SMTP_PASS", ""),
            smtp_to=os.getenv("HIVE_SMTP_TO", ""),
            slack_webhook=os.getenv("HIVE_SLACK_WEBHOOK", ""),
            discord_webhook=os.getenv("HIVE_DISCORD_WEBHOOK", ""),
            slack_bot_token=os.getenv("HIVE_SLACK_BOT_TOKEN", ""),
            slack_signing_secret=os.getenv("HIVE_SLACK_SIGNING_SECRET", ""),
            discord_bot_token=os.getenv("HIVE_DISCORD_BOT_TOKEN", ""),
            discord_public_key=os.getenv("HIVE_DISCORD_PUBLIC_KEY", ""),
            discord_application_id=os.getenv("HIVE_DISCORD_APP_ID", ""),
            smtp_from=os.getenv("HIVE_SMTP_FROM", ""),
            smtp_webhook_secret=os.getenv("HIVE_SMTP_WEBHOOK_SECRET", ""),
            slack_allowed_user_ids=_parse_csv_env("HIVE_SLACK_ALLOWED_USER_IDS"),
            discord_allowed_user_ids=_parse_csv_env("HIVE_DISCORD_ALLOWED_USER_IDS"),
            email_allowed_senders=frozenset(
                value.casefold() for value in _parse_csv_env("HIVE_EMAIL_ALLOWED_SENDERS")
            ),
            selfmod_proactive_interval=int(os.getenv("HIVE_SELFMOD_PROACTIVE_INTERVAL", "10")),
            selfmod_enable_safety_checks=os.getenv("HIVE_SELFMOD_ENABLE_SAFETY_CHECKS", "true").lower() == "true",
            selfmod_safety_max_files=int(os.getenv("HIVE_SELFMOD_SAFETY_MAX_FILES", "20")),
            selfmod_failure_cooldown_sec=float(os.getenv("HIVE_SELFMOD_FAILURE_COOLDOWN_SEC", "1800")),
            heartbeat_proactive_interval_sec=int(os.getenv("HIVE_HEARTBEAT_PROACTIVE_INTERVAL_SEC", "86400")),
            heartbeat_stale_fact_days=int(os.getenv("HIVE_HEARTBEAT_STALE_FACT_DAYS", "30")),
            heartbeat_stale_commitment_days=int(os.getenv("HIVE_HEARTBEAT_STALE_COMMITMENT_DAYS", "7")),
            deploy_ssh_host=os.getenv("HIVE_DEPLOY_SSH_HOST", ""),
            deploy_ssh_key=os.getenv("HIVE_DEPLOY_SSH_KEY", ""),
            entity_resolution_enabled=os.getenv("HIVE_ENTITY_RESOLUTION_ENABLED", "true").lower() == "true",
            entity_resolution_alias_map=os.getenv("HIVE_ENTITY_RESOLUTION_ALIAS_MAP", ""),
            stripe_secret_key=os.getenv("STRIPE_SECRET_KEY", ""),
            stripe_customer_id=os.getenv("STRIPE_CUSTOMER_ID", ""),
            budget_forecast_alert_days=int(os.getenv("HIVE_BUDGET_FORECAST_ALERT_DAYS", "1")),
            budget_daily_spend_cap_usd=float(os.getenv("HIVE_DAILY_SPEND_CAP_USD", "0")),
            approver_key=os.getenv("HIVE_APPROVER_KEY", ""),
        )

    def validate(self) -> list[str]:
        """Return a list of validation warnings/errors. Empty means OK."""
        issues = []
        if not self.exec_model:
            issues.append("HIVE_EXEC_MODEL is empty")
        if self.secret == "change_me":
            issues.append("HIVE_SECRET is the default 'change_me' — change it for production")
        if self.production_mode and self.secret == "change_me":
            issues.append("HIVE_PRODUCTION=true requires HIVE_SECRET to be changed from 'change_me'")
        if self.port < 1 or self.port > 65535:
            issues.append(f"HIVE_PORT={self.port} is out of range")
        if self.daily_call_cap < 1:
            issues.append("HIVE_DAILY_CALL_CAP must be >= 1")
        if self.exec_provider not in ("minimax", "anthropic"):
            issues.append(f"HIVE_EXEC_PROVIDER={self.exec_provider!r} must be 'minimax' or 'anthropic'")
        if self.shell_provider not in ("local", "docker"):
            issues.append(f"HIVE_SHELL_PROVIDER={self.shell_provider!r} must be 'local' or 'docker'")
        if self.max_iterations < 1:
            issues.append(f"HIVE_MAX_ITERATIONS={self.max_iterations} must be >= 1")
        if self.max_per_tool < 1:
            issues.append(f"HIVE_MAX_PER_TOOL={self.max_per_tool} must be >= 1")
        if self.selfmod_failure_threshold < 1:
            issues.append(f"HIVE_SELFMOD_THRESHOLD={self.selfmod_failure_threshold} must be >= 1")
        if self.exec_provider == "anthropic" and not self.anthropic_api_key:
            issues.append("HIVE_EXEC_PROVIDER=anthropic but ANTHROPIC_API_KEY is empty")
        if self.exec_provider == "minimax" and not self.minimax_api_key:
            issues.append("HIVE_EXEC_PROVIDER=minimax but MINIMAX_API_KEY is empty")
        if self.max_message_len < 1:
            issues.append("HIVE_MAX_MESSAGE_LEN must be >= 1")
        if self.ws_idle_timeout < 1:
            issues.append("HIVE_WS_IDLE_TIMEOUT must be >= 1 second")
        if self.selfmod_safety_max_files < 1:
            issues.append(f"HIVE_SELFMOD_SAFETY_MAX_FILES={self.selfmod_safety_max_files} must be >= 1")
        if self.autonomous_selfmod_enabled and not self.autonomy_enabled:
            issues.append("HIVE_AUTONOMOUS_SELFMOD_ENABLED requires HIVE_AUTONOMY_ENABLED=true")
        if self.autonomous_selfmod_enabled and not self.sandbox_image:
            issues.append(
                "HIVE_AUTONOMOUS_SELFMOD_ENABLED requires HIVE_SANDBOX_IMAGE to be configured"
            )
        if self.budget_forecast_alert_days < 0:
            issues.append("HIVE_BUDGET_FORECAST_ALERT_DAYS must be >= 0")
        if self.budget_daily_spend_cap_usd < 0:
            issues.append("HIVE_DAILY_SPEND_CAP_USD must be >= 0")
        if self.heartbeat_proactive_interval_sec < 0:
            issues.append(
                f"HIVE_HEARTBEAT_PROACTIVE_INTERVAL_SEC={self.heartbeat_proactive_interval_sec} must be >= 0"
            )
        if self.heartbeat_stale_fact_days < 1:
            issues.append(
                f"HIVE_HEARTBEAT_STALE_FACT_DAYS={self.heartbeat_stale_fact_days} must be >= 1"
            )
        if self.heartbeat_stale_commitment_days < 1:
            issues.append(
                f"HIVE_HEARTBEAT_STALE_COMMITMENT_DAYS={self.heartbeat_stale_commitment_days} must be >= 1"
            )
        return issues

    def ensure_dirs(self) -> None:
        """Create runtime dirs. Called explicitly by the builder/doctor, never at import."""
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def llm_summary(self) -> dict:
        """Return a dict summarising the active LLM configuration (no secrets)."""
        return {
            "exec_provider": self.exec_provider,
            "exec_model": self.exec_model,
            "exec_fallback_model": self.exec_fallback_model,
            "aux_model": self.aux_model,
            "planner_enabled": self.planner_enabled,
            "daily_call_cap": self.daily_call_cap,
            "max_iterations": self.max_iterations,
            "max_per_tool": self.max_per_tool,
            "autonomy_enabled": self.autonomy_enabled,
            "autonomous_selfmod_enabled": self.autonomous_selfmod_enabled,
        }

    def is_production(self) -> bool:
        """Return explicit production mode or the legacy non-loopback heuristic."""
        return self.production_mode or (
            self.secret != "change_me" and self.host not in ("127.0.0.1", "localhost")
        )

    def to_safe_dict(self) -> dict:
        """Return config as a dict with all secret fields redacted."""
        _REDACTED = "***"
        return {
            "exec_provider": self.exec_provider,
            "exec_model": self.exec_model,
            "exec_fallback_model": self.exec_fallback_model,
            "aux_model": self.aux_model,
            "minimax_anthropic_base": self.minimax_anthropic_base,
            "planner_enabled": self.planner_enabled,
            "planner_timeout": self.planner_timeout,
            "daily_call_cap": self.daily_call_cap,
            "window_warn_pct": self.window_warn_pct,
            "host": self.host,
            "port": self.port,
            "secret": _REDACTED,
            "approver_key": _REDACTED if self.approver_key else "",
            "minimax_api_key": _REDACTED if self.minimax_api_key else "",
            "anthropic_api_key": _REDACTED if self.anthropic_api_key else "",
            "github_token": _REDACTED if self.github_token else "",
            "telegram_token": _REDACTED if self.telegram_token else "",
            "telegram_webhook_secret": _REDACTED if self.telegram_webhook_secret else "",
            "slack_bot_token": _REDACTED if self.slack_bot_token else "",
            "slack_signing_secret": _REDACTED if self.slack_signing_secret else "",
            "discord_bot_token": _REDACTED if self.discord_bot_token else "",
            "discord_public_key": _REDACTED if self.discord_public_key else "",
            "smtp_pass": _REDACTED if self.smtp_pass else "",
            "smtp_webhook_secret": _REDACTED if self.smtp_webhook_secret else "",
            "heartbeat_sec": self.heartbeat_sec,
            "max_concurrent_agents": self.max_concurrent_agents,
            "max_iterations": self.max_iterations,
            "max_per_tool": self.max_per_tool,
            "selfmod_failure_threshold": self.selfmod_failure_threshold,
            "tool_timeout": self.tool_timeout,
            "mcp_servers": list(self.mcp_servers),
            "sandbox_image": self.sandbox_image,
            "shell_provider": self.shell_provider,
            "shell_docker_image": self.shell_docker_image,
            "cors_origins": self.cors_origins,
            "max_message_len": self.max_message_len,
            "ws_idle_timeout": self.ws_idle_timeout,
            "budget_forecast_alert_days": self.budget_forecast_alert_days,
            "budget_daily_spend_cap_usd": self.budget_daily_spend_cap_usd,
            "is_production": self.is_production(),
        }


_CONFIG: HiveConfig | None = None


def get_config() -> HiveConfig:
    """Process-wide config, built once on first access."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = HiveConfig.from_env()
    return _CONFIG


def set_config(cfg: HiveConfig) -> None:
    """Inject an explicit config (HiveOS.build wiring; test isolation)."""
    global _CONFIG
    _CONFIG = cfg
