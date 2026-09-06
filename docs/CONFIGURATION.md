# HiveOS — Configuration Reference

All configuration is read from environment variables by `HiveConfig.from_env()` in
`src/hive/core/config.py`. The config is a frozen dataclass — no import-time side
effects, no mutation after construction. `HiveOS.build()` calls it once.

## Precedence (highest → lowest)

```
1. credentials vault  (data/credentials.json, 0o600)
2. .env file          (loaded by HiveConfig.from_env via python-dotenv)
3. shell environment  (already-set os.environ values)
4. code defaults      (HiveConfig field defaults)
```

`credentials.inject()` is called at build time and writes vault values into `os.environ`
**only if the key is not already set**. This means a value already in the shell environment
wins over the vault, but the vault wins over `.env` file values (because `inject` runs
before `from_env` reads env vars, and shell vars pre-exist both). In practice: keep API
keys in the vault on production, keep overrides in the shell for temporary testing.

Copy `.env.example` to `.env` and edit before starting:

```bash
cp .env.example .env
```

---

## Required minimum (local dev)

| Variable | Example | Notes |
|---|---|---|
| `MINIMAX_API_KEY` | `eyJ...` | MiniMax API key (Token Plan or PAYG) |
| `HIVE_SECRET` | `my-secret-token` | Bearer token for all authenticated gateway endpoints |

With only these two set, `hive ask "hello"` works. Everything else has a working default.

---

## Executor model (`llm/`)

| Variable | Default | Notes |
|---|---|---|
| `HIVE_EXEC_PROVIDER` | `minimax` | `minimax` or `anthropic` — selects which adapter the router uses |
| `HIVE_EXEC_MODEL` | `MiniMax-M3` | Primary model string (passed verbatim to the adapter) |
| `HIVE_EXEC_FALLBACK_MODEL` | `MiniMax-M2.7` | Failover model when primary is rate-limited or errors |
| `HIVE_AUX_MODEL` | `MiniMax-M2.7` | Cheaper model for summarisation, titling, memory consolidation |

### MiniMax executor (default)

| Variable | Default | Notes |
|---|---|---|
| `MINIMAX_API_KEY` | *(required)* | API key; may be comma-separated for multi-key pool (auto-rotates on 429) |
| `MINIMAX_ANTHROPIC_BASE` | `https://api.minimax.io/anthropic` | Anthropic-compatible endpoint for chat completions with interleaved thinking |
| `MINIMAX_OPENAI_BASE` | `https://api.minimax.io/v1` | OpenAI-compatible endpoint (used by Codex planner subprocess) |
| `HIVE_REMAINS_URL` | `https://api.minimax.io/v1/token_plan/remains` | Endpoint polled by the budgeter to self-calibrate the rolling token window |

### Anthropic executor (alternative)

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(required if provider=anthropic)* | Anthropic API key; may be comma-separated |
| `ANTHROPIC_BASE` | `https://api.anthropic.com` | Override for Anthropic-compatible proxies |

---

## Planner (`llm/planner`)

The planner is the **thinking-only** path — it plans but never executes. Uses ChatGPT Plus
via Codex OAuth subprocess. Disabled by default; enable when you need deep architecture work.

| Variable | Default | Notes |
|---|---|---|
| `HIVE_PLANNER_ENABLED` | `false` | Set `true` to enable the thinking model for novel/complex tasks |
| `HIVE_PLANNER_CMD` | `codex exec` | Shell command that invokes the planner (must be on `PATH`) |
| `HIVE_PLANNER_TIMEOUT` | `120` | Seconds before the planner subprocess is killed |

---

## Budget guard (`core/budgeter`)

| Variable | Default | Notes |
|---|---|---|
| `HIVE_DAILY_CALL_CAP` | `3000` | Hard daily call ceiling (counts completions, not tokens) |
| `HIVE_WINDOW_WARN_PCT` | `70` | Warn (but don't block) when token-window usage exceeds this percentage |

The budgeter polls `HIVE_REMAINS_URL` to track the rolling token window and records per-call
cost from `INFERENCE_END` events. Snapshot available at `GET /budget`.

---

## Gateway (`gateway/app`)

| Variable | Default | Notes |
|---|---|---|
| `HIVE_HOST` | `0.0.0.0` | FastAPI bind address |
| `HIVE_PORT` | `8088` | FastAPI bind port |
| `HIVE_SECRET` | `change_me` | Bearer token for all `/chat`, `/budget`, `/approvals`, `/telemetry`, `/audit`, `/tasks`, `/traces` endpoints |
| `HIVE_PRODUCTION` | `false` | Set `true` for a deployed gateway; startup then rejects the default `HIVE_SECRET=change_me`. |
| `HIVE_CORS_ORIGINS` | `*` | CORS allowed origins (comma-separated or `*` for all). Restrict to your domain in production. |
| `HIVE_MAX_MESSAGE_LEN` | `32000` | Maximum chat message length in characters. Requests exceeding this return HTTP 422. |
| `HIVE_WS_IDLE_TIMEOUT` | `300` | WebSocket idle timeout in seconds. Connections with no messages close after this duration. |

---

## Storage

| Variable | Default | Notes |
|---|---|---|
| `HIVE_DATA_DIR` | `<repo>/data` | Directory for all runtime state (SQLite, audit log, backups) |
| `HIVE_STATE_DB` | `<data_dir>/hive.sqlite` | Shared SQLite database (sessions, memory, tasks, cron, commitments) |

---

## Memory (`memory/`)

| Variable | Default | Notes |
|---|---|---|
| `MNEMOSYNE_HOME` | `<data_dir>/mnemosyne` | Where the Mnemosyne package stores its SQLite databases (when `memory` extra is installed) |
| `MNEMOSYNE_MCP_URL` | *(empty)* | HTTP(S) URL of a remote Mnemosyne MCP SSE server; loaded automatically as an MCP server at gateway startup |
| `OBSIDIAN_VAULT_PATH` | `<repo>/vault` | Root of the Obsidian vault for long-term markdown notes |

**Note:** Without the `memory` extra (`pip install -e ".[memory]"`), HiveOS falls back to
`LocalMemoryProvider` (SQLite, no semantic search). All APIs remain compatible.

---

## Autonomy (`autonomy/`)

| Variable | Default | Notes |
|---|---|---|
| `HIVE_AUTONOMY_ENABLED` | `false` | Master opt-in for heartbeat scheduling, dispatch and consolidation. Leave `false` until backup/recovery and approval gates are verified. |
| `HIVE_AUTONOMOUS_SELFMOD_ENABLED` | `false` | Separate opt-in for heartbeat-triggered self-diagnosis/self-modification; requires `HIVE_AUTONOMY_ENABLED=true`. |
| `HIVE_HEARTBEAT_SEC` | `900` | Seconds between heartbeat ticks (15 min default; reduce for testing) |
| `HIVE_MAX_AGENTS` | `3` | Maximum concurrent subagents during task dispatch (concurrency cap) |

Run `hive heartbeat` only after explicitly enabling the master gate. The self-modification gate must remain off through the durability and approval-recovery rollout; a running heartbeat alone must not create changes.

---

## Agent limits (`core/loop_guard`, `tools/`)

These four variables control the agent's safety bounds. All have sane defaults that work
out-of-the-box; tune them for your workload without editing code.

| Variable | Default | Notes |
|---|---|---|
| `HIVE_MAX_ITERATIONS` | `30` | Maximum tool-loop iterations per single turn; prevents runaway tool calls |
| `HIVE_MAX_PER_TOOL` | `50` | Maximum calls to any single tool per session; prevents tool-abuse loops |
| `HIVE_SELFMOD_THRESHOLD` | `3` | Consecutive failure count before self-improvement analysis triggers automatically |
| `HIVE_TOOL_TIMEOUT` | `60` | Seconds before a single tool call is cancelled (prevents hanging tools) |
| `HIVE_SHELL_PROVIDER` | `local` | Shell execution backend: `local` (host process) or `docker` (disposable container with network isolation) |
| `HIVE_SHELL_DOCKER_IMAGE` | `alpine:latest` | Docker image used when `HIVE_SHELL_PROVIDER=docker` |

---

## GitHub identity

Required for `SelfModifier` to open draft PRs automatically. Without this, Hive pushes
the branch but a human must open the PR manually.

| Variable | Default | Notes |
|---|---|---|
| `HIVE_GITHUB_TOKEN` | *(empty)* | Fine-grained PAT or GitHub App token with `contents:write` and `pull_requests:write` |
| `HIVE_GITHUB_OWNER` | *(empty)* | GitHub username or org that owns the repo (e.g. `hiveosagent`) |
| `HIVE_GITHUB_REPO` | *(empty)* | Repository name (e.g. `hiveos`) |

---

## Telegram surface

Optional. Set to enable the Telegram webhook endpoint and `external_message` tool.

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(empty)* | BotFather token; also activates `ExternalMessage` tool (telegram channel) |
| `TELEGRAM_WEBHOOK_SECRET` | *(empty)* | Required when `TELEGRAM_BOT_TOKEN` enables the inbound webhook; validated from `X-Telegram-Bot-Api-Secret-Token`. |
| `HIVE_TELEGRAM_ALLOWED_USER_IDS` | *(empty)* | Comma-separated Telegram user IDs. At least this or the chat allowlist is required for the inbound webhook. |
| `HIVE_TELEGRAM_ALLOWED_CHAT_IDS` | *(empty)* | Comma-separated Telegram chat IDs. At least this or the user allowlist is required for the inbound webhook. |

---

## Multi-channel messaging (Email + Slack)

The `external_message` tool supports `channel="email"` and `channel="slack"` in addition to the
default Telegram. All six variables must be set for the respective channel to work.

| Variable | Default | Notes |
|---|---|---|
| `HIVE_SMTP_HOST` | *(empty)* | SMTP server hostname, e.g. `smtp.gmail.com`. Leave empty → email disabled. |
| `HIVE_SMTP_PORT` | `587` | SMTP port (587 = STARTTLS; 465 = SSL requires custom code). |
| `HIVE_SMTP_USER` | *(empty)* | SMTP login username / From address. |
| `HIVE_SMTP_PASS` | *(empty)* | SMTP password or app-password (use app-password for Gmail). |
| `HIVE_SMTP_TO` | *(empty)* | Recipient address for Hive-generated emails. |
| `HIVE_SLACK_WEBHOOK` | *(empty)* | Slack incoming webhook URL. Leave empty → Slack disabled. |

### Inbound sender allowlists

Inbound webhooks authenticate the platform request and separately authorize the
human sender. Every enabled inbound Slack, Discord, or email surface fails
closed at startup unless its corresponding allowlist is configured. A request
from a sender outside the list is acknowledged without reaching `hive.ask()`.

| Variable | Required when | Notes |
|---|---|---|
| `HIVE_SLACK_ALLOWED_USER_IDS` | `HIVE_SLACK_SIGNING_SECRET` is set | Comma-separated Slack user IDs from the signed event payload. |
| `HIVE_DISCORD_ALLOWED_USER_IDS` | `HIVE_DISCORD_PUBLIC_KEY` is set | Comma-separated Discord user IDs from the verified interaction. |
| `HIVE_EMAIL_ALLOWED_SENDERS` | `HIVE_SMTP_WEBHOOK_SECRET` is set | Comma-separated sender email addresses; compared case-insensitively. The authenticated ingress must strip inbound `Authentication-Results`, add its own aligned `dmarc=pass` result, and send `X-Verified-Sender` matching `From`. |

The email webhook secret authenticates the configured ingress, not the RFC822
`From` field. The ingress therefore owns sender verification: it must remove
untrusted authentication headers before forwarding the message and must only
set `X-Verified-Sender` after provider-verified DMARC alignment. Hive rejects
email before `hive.ask()` unless both assertions are present and consistent.

---

## Self-mod sandbox

| Variable | Default | Notes |
|---|---|---|
| `HIVE_SANDBOX_IMAGE` | *(empty)* | Docker image for running candidate test suites in isolation (e.g. `python:3.12`). Leave empty to run tests locally. |

When set, any AUTO-tier self-mod edit runs `pytest` inside the container before pushing.
The container gets `--network none` and a read-only worktree bind-mount.

---

## MCP servers

| Variable | Default | Notes |
|---|---|---|
| `HIVE_MCP_SERVERS` | *(empty)* | Semicolon-separated list of MCP server specs loaded at gateway startup |

**Spec formats:**
- `npx -y @modelcontextprotocol/server-github` — stdio command; HiveOS spawns the process
- `https://mnemosyne.example.com/mcp` — HTTP(S) URL with SSE transport
- `MNEMOSYNE_MCP_URL` is loaded automatically in addition to this list

---

## Model pricing overrides

Fine-tune cost accounting for non-standard models or pricing tiers:

| Variable | Example | Notes |
|---|---|---|
| `HIVE_PRICE_<MODEL>_IN` | `HIVE_PRICE_MiniMax-M3_IN=0.3` | Input price in USD per 1M tokens for `<MODEL>` |
| `HIVE_PRICE_<MODEL>_OUT` | `HIVE_PRICE_MiniMax-M3_OUT=1.2` | Output price in USD per 1M tokens for `<MODEL>` |

Replace `/` and `-` in model names with `_` when constructing the env var name.

---

## Credentials vault

In addition to env vars, HiveOS loads secrets from a `0o600` vault file managed by
`core/credentials.py`. Use `credentials.save(key, value)` to write; `credentials.inject()`
is called at build time and populates env vars from the vault without overwriting existing ones.

Vault location: `<data_dir>/credentials.json` (owner-only, `chmod 600`).

This is the recommended way to store API keys on production — edit `.env` only for bootstrap.

---

## Full variable summary

| Variable | Required | Default | Subsystem |
|---|---|---|---|
| `MINIMAX_API_KEY` | ✓ (default provider) | — | llm/minimax |
| `HIVE_SECRET` | ✓ | `change_me` | gateway auth |
| `HIVE_EXEC_PROVIDER` | | `minimax` | llm/router |
| `HIVE_EXEC_MODEL` | | `MiniMax-M3` | llm/router |
| `HIVE_EXEC_FALLBACK_MODEL` | | `MiniMax-M2.7` | llm/failover |
| `HIVE_AUX_MODEL` | | `MiniMax-M2.7` | llm/router (aux) |
| `MINIMAX_ANTHROPIC_BASE` | | `https://api.minimax.io/anthropic` | llm/minimax |
| `MINIMAX_OPENAI_BASE` | | `https://api.minimax.io/v1` | llm/minimax |
| `ANTHROPIC_API_KEY` | ✓ (if provider=anthropic) | — | llm/anthropic |
| `ANTHROPIC_BASE` | | `https://api.anthropic.com` | llm/anthropic |
| `HIVE_PLANNER_ENABLED` | | `false` | llm/planner |
| `HIVE_PLANNER_CMD` | | `codex exec` | llm/planner |
| `HIVE_PLANNER_TIMEOUT` | | `120` | llm/planner |
| `HIVE_REMAINS_URL` | | MiniMax token plan URL | llm/budgeter |
| `HIVE_DAILY_CALL_CAP` | | `3000` | core/budgeter |
| `HIVE_WINDOW_WARN_PCT` | | `70` | core/budgeter |
| `HIVE_HOST` | | `0.0.0.0` | gateway |
| `HIVE_PORT` | | `8088` | gateway |
| `HIVE_CORS_ORIGINS` | | `*` | gateway/cors |
| `HIVE_MAX_MESSAGE_LEN` | | `32000` | gateway/chat |
| `HIVE_WS_IDLE_TIMEOUT` | | `300` | gateway/ws |
| `MNEMOSYNE_HOME` | | `<data>/mnemosyne` | memory |
| `MNEMOSYNE_MCP_URL` | | — | memory/mcp |
| `OBSIDIAN_VAULT_PATH` | | `<repo>/vault` | memory/vault |
| `HIVE_DATA_DIR` | | `<repo>/data` | storage |
| `HIVE_STATE_DB` | | `<data>/hive.sqlite` | storage |
| `HIVE_AUTONOMY_ENABLED` | | `false` | autonomy/master gate |
| `HIVE_AUTONOMOUS_SELFMOD_ENABLED` | | `false` | autonomy/self-mod gate |
| `HIVE_HEARTBEAT_SEC` | | `900` | autonomy |
| `HIVE_MAX_AGENTS` | | `3` | agents/delegate |
| `HIVE_MAX_ITERATIONS` | | `30` | core/loop_guard |
| `HIVE_MAX_PER_TOOL` | | `50` | core/loop_guard |
| `HIVE_SELFMOD_THRESHOLD` | | `3` | core/self_mod |
| `HIVE_TOOL_TIMEOUT` | | `60` | tools/ |
| `HIVE_SHELL_PROVIDER` | | `local` | tools/shell_provider |
| `HIVE_SHELL_DOCKER_IMAGE` | | `alpine:latest` | tools/shell_provider |
| `HIVE_GITHUB_TOKEN` | | — | core/self_mod |
| `HIVE_GITHUB_OWNER` | | — | core/self_mod |
| `HIVE_GITHUB_REPO` | | — | core/self_mod |
| `TELEGRAM_BOT_TOKEN` | | — | gateway/telegram |
| `TELEGRAM_WEBHOOK_SECRET` | | — | gateway/telegram |
| `HIVE_SMTP_HOST` | | — | tools/builtins (email) |
| `HIVE_SMTP_PORT` | 587 | — | tools/builtins (email) |
| `HIVE_SMTP_USER` | | — | tools/builtins (email) |
| `HIVE_SMTP_PASS` | | — | tools/builtins (email) |
| `HIVE_SMTP_TO` | | — | tools/builtins (email) |
| `HIVE_SLACK_WEBHOOK` | | — | tools/builtins (slack) |
| `HIVE_SANDBOX_IMAGE` | | — | core/sandbox |
| `HIVE_MCP_SERVERS` | | — | tools/mcp |
| `HIVE_PRICE_<MODEL>_IN` | | catalog default | llm/pricing |
| `HIVE_PRICE_<MODEL>_OUT` | | catalog default | llm/pricing |
| `HIVE_LIVE_TEST` | | — | tests (smoke only) |

---

## Common configurations

### Minimal (local development — no autonomy, no GitHub)

```bash
MINIMAX_API_KEY=your_key_here
HIVE_SECRET=dev-secret
```

Gives you: `hive ask`, `hive chat`, `hive serve` (full API, no auth issues). All
observability endpoints work. Self-mod will not push branches (no GitHub token).

### Local dev with full features

```bash
MINIMAX_API_KEY=your_key_here
HIVE_SECRET=dev-secret
HIVE_GITHUB_TOKEN=ghp_...
HIVE_GITHUB_OWNER=yourname
HIVE_GITHUB_REPO=hiveos
TELEGRAM_BOT_TOKEN=12345:ABC...    # optional — enables /telegram/webhook + external_message
HIVE_SANDBOX_IMAGE=python:3.12     # optional — isolates self-mod tests in Docker
HIVE_HEARTBEAT_SEC=60              # shorter ticks for testing
```

### Production VPS (complete)

```bash
# Executor
MINIMAX_API_KEY=key1,key2          # comma-split for multi-key failover
HIVE_SECRET=<32-char-random>       # generate with: python -c "import secrets; print(secrets.token_hex(16))"
HIVE_EXEC_PROVIDER=minimax
HIVE_EXEC_MODEL=MiniMax-M3
HIVE_AUX_MODEL=MiniMax-M2.7

# Gateway
HIVE_HOST=127.0.0.1                # nginx proxies; bind only loopback
HIVE_PORT=8088

# Memory
MNEMOSYNE_HOME=/opt/hiveos/data/mnemosyne

# Autonomy — both gates remain false until backup/recovery and approval checks are verified.
HIVE_AUTONOMY_ENABLED=false
HIVE_AUTONOMOUS_SELFMOD_ENABLED=false
HIVE_HEARTBEAT_SEC=900
HIVE_MAX_AGENTS=3

# GitHub (self-mod PRs)
HIVE_GITHUB_TOKEN=ghp_...
HIVE_GITHUB_OWNER=hiveosagent
HIVE_GITHUB_REPO=hiveos

# Telegram
TELEGRAM_BOT_TOKEN=12345:ABC...
TELEGRAM_WEBHOOK_SECRET=<random>

# Self-mod sandbox
HIVE_SANDBOX_IMAGE=python:3.12
```

See [`docs/DEPLOYMENT.md`](DEPLOYMENT.md) for the full production setup guide.

---

---

## Voice surface (`surfaces/voice`)

The voice surface requires audio libraries that are not installed by default (they need
native system binaries like ALSA/PulseAudio on Linux or CoreAudio on macOS).

```bash
# Install voice dependencies
pip install -e ".[voice]"

# System packages (Ubuntu/Debian):
sudo apt install libportaudio2 espeak-ng
```

**Start the voice surface:**

```bash
hive voice
```

The surface lazy-imports `faster-whisper` (speech-to-text), `piper-tts` (text-to-speech),
and `sounddevice` (audio I/O). If any is missing, the surface prints a clear installation
message instead of crashing the gateway.

---

## See also

- [`docs/DEPLOYMENT.md`](DEPLOYMENT.md) — production VPS setup
- [`docs/DEVELOPMENT.md`](DEVELOPMENT.md) — local dev guide
- [`docs/decisions/002-minimax-as-executor.md`](decisions/002-minimax-as-executor.md) — why MiniMax
- [`docs/decisions/001-sqlite-first.md`](decisions/001-sqlite-first.md) — why SQLite
