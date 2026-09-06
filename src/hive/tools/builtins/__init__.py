"""
builtins — Hive's built-in tools (KEEP from Tools/registry.py builtins).

Safe: read_file / write_file / shell / web_get. Dangerous (always gated by the
executor via the PROTECTED approval gate): spend_money / deploy / external_message.
Destructive shell/file actions are caught by gate.is_dangerous even though `shell`
is not flagged dangerous itself, so routine commands stay fast.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import urllib.parse
from pathlib import Path
from typing import Any

import httpcore
import httpx

from hive.core.types import ToolResult
from hive.tools import discovery as _discovery
from hive.tools import introspect as _introspect
from hive.tools.base import BaseTool, ToolSpec
from hive.tools.registry import ToolRegistry
from hive.tools.shell_provider import LocalShellProvider, ShellProvider

_BLOCKED_HOSTS = frozenset({
    "localhost", "localhost.localdomain", "metadata", "metadata.google.com",
    "metadata.google.internal", "metadata.azure.internal", "instance-data.ec2.internal",
})
_BLOCKED_HOST_SUFFIXES = (".internal", ".localhost")
_MAX_WEB_GET_BYTES = 12_000

# Local AST fast-path: skip the web search when the top AST hit scores above this.
_LOCAL_SCORE_THRESHOLD = 0.8


def _canonical_host(host: str) -> str:
    host = host.strip().rstrip(".").casefold()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Invalid hostname") from exc


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse regular and alternate IPv4 spellings plus IPv6 literals."""
    host = host.strip().strip("[]")
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass

    value: int | None = None
    if host.lower().startswith("0x"):
        try:
            value = int(host, 16)
        except ValueError:
            return None
    elif host.isdigit():
        try:
            value = int(host, 10)
            if value > 0xFFFFFFFF and host.startswith("0"):
                value = int(host, 8)
        except ValueError:
            return None
        if value > 0xFFFFFFFF and host.startswith("0"):
            try:
                value = int(host, 8)
            except ValueError:
                return None
    else:
        parts = host.split(".")
        if len(parts) != 4:
            return None
        values: list[int] = []
        for part in parts:
            try:
                if part.lower().startswith("0x"):
                    parsed = int(part, 16)
                elif len(part) > 1 and part.startswith("0"):
                    parsed = int(part, 8)
                else:
                    parsed = int(part, 10)
            except ValueError:
                return None
            if not 0 <= parsed <= 255:
                return None
            values.append(parsed)
        value = int.from_bytes(bytes(values), "big")

    if value is not None and 0 <= value <= 0xFFFFFFFF:
        return ipaddress.IPv4Address(value)
    return None


def _validate_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    # is_global covers most non-public ranges. Multicast and reserved ranges are
    # deliberately explicit because some Python releases classify them as global.
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise ValueError(f"Blocked non-global address: {address}")


def _resolve_host_addresses(host: str, port: int) -> tuple[str, ...]:
    canonical = _canonical_host(host)
    if canonical in _BLOCKED_HOSTS or canonical.endswith(_BLOCKED_HOST_SUFFIXES):
        raise ValueError(f"Blocked hostname: {canonical}")

    literal = _parse_ip_literal(canonical)
    if literal is not None:
        _validate_address(literal)
        return (str(literal),)

    try:
        records = socket.getaddrinfo(canonical, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Unable to resolve hostname: {canonical}") from exc

    addresses: list[str] = []
    for record in records:
        raw_address = record[4][0]
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise ValueError(f"Resolver returned an invalid address for {canonical}") from exc
        _validate_address(address)
        text = str(address)
        if text not in addresses:
            addresses.append(text)
    if not addresses:
        raise ValueError(f"Hostname resolved without addresses: {canonical}")
    return tuple(addresses)


def _validate_url(url: str) -> None:
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid URL") from exc
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Blocked scheme: {parsed.scheme!r}")
    if parsed.username or parsed.password:
        raise ValueError("URL userinfo not allowed")
    host = parsed.hostname
    if not host:
        raise ValueError("URL hostname required")
    _resolve_host_addresses(host, port or (443 if parsed.scheme == "https" else 80))


async def _check_redirect(response: httpx.Response) -> None:
    """httpx event hook: block redirects to private/loopback addresses."""
    if response.is_redirect:
        location = response.headers.get("location", "")
        if location:
            try:
                try:
                    base_url = str(response.url)
                except RuntimeError:
                    base_url = ""
                _validate_url(urllib.parse.urljoin(base_url, location))
            except ValueError as exc:
                raise ValueError(f"SSRF redirect blocked: {exc}") from exc


async def _read_limited_response(response: httpx.Response) -> tuple[str, bool]:
    """Read at most the tool's output budget without buffering an entire body."""
    body = bytearray()
    truncated = False
    async for chunk in response.aiter_bytes(chunk_size=_MAX_WEB_GET_BYTES):
        remaining = _MAX_WEB_GET_BYTES - len(body)
        if remaining <= 0:
            truncated = True
            break
        body.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
            break
    return body.decode(response.encoding or "utf-8", errors="replace"), truncated


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect only to addresses validated immediately before each TCP connect."""

    def __init__(self) -> None:
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        addresses = await asyncio.to_thread(_resolve_host_addresses, host, port)
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._backend.connect_tcp(address, port, **kwargs)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(self, path: str, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        return await self._backend.connect_unix_socket(path, **kwargs)

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PinnedHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport with DNS resolution pinned and environment proxies disabled."""

    def __init__(self) -> None:
        super().__init__(trust_env=False)
        self._pool._network_backend = _PinnedNetworkBackend()


class ReadFile(BaseTool):
    spec = ToolSpec(
        name="read_file", description="Read a UTF-8 text file (truncated).",
        parameters={"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"]}, category="files")

    async def execute(self, **params: Any) -> ToolResult:
        path = str(params.get("path", ""))
        text = Path(path).read_text(encoding="utf-8", errors="replace")[:20_000]
        return ToolResult(tool_name="read_file", content=text)


class WriteFile(BaseTool):
    spec = ToolSpec(
        name="write_file", description="Write a UTF-8 text file (creates parents).",
        parameters={"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["w", "a"], "default": "w"}},
            "required": ["path", "content"]}, category="files")

    async def execute(self, **params: Any) -> ToolResult:
        path = str(params.get("path", ""))
        content = str(params.get("content", ""))
        mode = str(params.get("mode", "w"))
        if mode not in {"w", "a"}:
            return ToolResult(tool_name="write_file", content=f"invalid mode: {mode}", success=False)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open(mode, encoding="utf-8") as f:
            f.write(content)
        action = "appended" if mode == "a" else "wrote"
        return ToolResult(tool_name="write_file", content=f"{action} {len(content)} chars to {path}")


class DeleteFile(BaseTool):
    spec = ToolSpec(
        name="delete_file",
        description="Delete a file (subject to path safety checks).",
        parameters={"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"]},
        category="files",
    )

    async def execute(self, **params: Any) -> ToolResult:
        path = str(params.get("path", ""))
        p = Path(path)
        if not p.exists():
            return ToolResult(tool_name="delete_file", content=f"file not found: {path}", success=False)
        p.unlink()
        return ToolResult(tool_name="delete_file", content=f"deleted: {path}")


class Shell(BaseTool):
    spec = ToolSpec(
        name="shell", description="Run a shell command (destructive commands are gated).",
        parameters={"type": "object", "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"]}, category="system")

    def __init__(self, provider: ShellProvider | None = None) -> None:
        self._provider = provider or LocalShellProvider()

    async def execute(self, **params: Any) -> ToolResult:
        cmd = str(params.get("cmd", ""))
        result = await self._provider.run(cmd)
        return ToolResult(tool_name="shell", content=result.stdout[:8_000],
                          success=result.returncode == 0)


class WebGet(BaseTool):
    spec = ToolSpec(
        name="web_get", description="HTTP GET a URL and return text (truncated).",
        parameters={"type": "object", "properties": {"url": {"type": "string"}},
                    "required": ["url"]}, category="web")

    async def execute(self, **params: Any) -> ToolResult:
        url = str(params.get("url", ""))
        try:
            await asyncio.to_thread(_validate_url, url)
        except ValueError as exc:
            return ToolResult(tool_name="web_get", content=f"[blocked: {exc}]", success=False)
        try:
            async with httpx.AsyncClient(
                timeout=30,
                follow_redirects=True,
                max_redirects=10,
                event_hooks={"response": [_check_redirect]},
                transport=_PinnedHTTPTransport(),
                trust_env=False,
            ) as c:
                async with c.stream("GET", url) as r:
                    content, truncated = await _read_limited_response(r)
                    if truncated:
                        content += "\n[response truncated at 12000 bytes]"
                    return ToolResult(tool_name="web_get", content=content,
                                      success=r.is_success)
        except ValueError as exc:
            return ToolResult(tool_name="web_get", content=f"[blocked: {exc}]", success=False)
        except httpx.HTTPError as exc:
            return ToolResult(tool_name="web_get", content=f"[request failed: {exc}]", success=False)


class _Gated(BaseTool):
    """Dangerous tools: real side effects live in the surface; here they confirm intent."""
    _name = ""
    _desc = ""

    def __init__(self) -> None:
        self.spec = ToolSpec(name=self._name, description=self._desc, dangerous=True,
                             category="gated")


class StripeAdapter:
    """Minimal Stripe PaymentIntent adapter used by SpendMoney."""

    _API = "https://api.stripe.com/v1/payment_intents"

    def __init__(self, secret_key: str, customer_id: str = "") -> None:
        self._key = secret_key
        self._customer = customer_id

    async def charge(self, amount_usd: float, description: str) -> dict:
        """Create a Stripe PaymentIntent and return the response dict."""
        amount_cents = max(1, int(round(amount_usd * 100)))
        payload: dict[str, str] = {
            "amount": str(amount_cents),
            "currency": "usd",
            "description": description[:500],
            "automatic_payment_methods[enabled]": "true",
        }
        if self._customer:
            payload["customer"] = self._customer
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self._API,
                headers={"Authorization": f"Bearer {self._key}"},
                data=payload,
            )
        resp.raise_for_status()
        return resp.json()


class SpendMoney(_Gated):
    _name, _desc = "spend_money", "Spend money via Stripe (requires approval)."

    def __init__(self, stripe_key: str = "", stripe_customer: str = "") -> None:
        super().__init__()
        self._stripe_key = stripe_key
        self._stripe_customer = stripe_customer

    async def execute(self, **params: Any) -> ToolResult:
        import re as _re
        what = str(params.get("what", ""))
        amount_str = str(params.get("amount", "0"))
        if not self._stripe_key:
            return ToolResult(
                tool_name="spend_money",
                content=(
                    f"[spend_money: no payment backend configured; "
                    f"requested: {amount_str} for '{what}'. "
                    f"Wire STRIPE_SECRET_KEY to enable the Stripe adapter.]"
                ),
            )
        try:
            amount = float(_re.sub(r"[^\d.]", "", amount_str) or "0")
            adapter = StripeAdapter(self._stripe_key, self._stripe_customer)
            result = await adapter.charge(amount, what)
            pi_id = result.get("id", "?")
            status = result.get("status", "?")
            return ToolResult(
                tool_name="spend_money", success=True,
                content=f"PaymentIntent {pi_id}: {status} (${amount:.2f} for '{what}')",
            )
        except Exception as exc:
            return ToolResult(
                tool_name="spend_money", success=False,
                content=f"[spend_money failed: {exc}]",
            )


_SAFE_DEPLOY_TARGETS = {"gateway", "orchestrator", "keeper"}


class Deploy(_Gated):
    _name = "deploy"
    _desc = "Deploy a service via systemctl (local), Docker, or SSH (requires approval)."

    def __init__(self, ssh_host: str = "", ssh_key: str = "") -> None:
        self.spec = ToolSpec(
            name=self._name,
            description=self._desc,
            dangerous=True,
            category="gated",
            parameters={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Service target: gateway, orchestrator, or keeper",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["systemctl", "docker", "ssh"],
                        "default": "systemctl",
                        "description": "Deployment method",
                    },
                    "container": {
                        "type": "string",
                        "description": "Docker container name (docker mode only; defaults to hiveos-{target})",
                    },
                },
                "required": ["target"],
            },
        )
        self._ssh_host = ssh_host
        self._ssh_key = ssh_key

    async def _run_cmd(self, cmd: str, timeout: float = 30.0) -> ToolResult:
        import asyncio as _asyncio
        proc = await _asyncio.create_subprocess_shell(
            cmd,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await _asyncio.wait_for(proc.communicate(), timeout=timeout)
        except _asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return ToolResult(tool_name="deploy", content=f"timeout after {timeout}s", success=False)
        text = out.decode(errors="replace").strip() if out else ""
        ok = proc.returncode == 0
        status = "ok" if ok else f"exit {proc.returncode}"
        return ToolResult(tool_name="deploy", success=ok,
                          content=f"{status}\n{text}".strip())

    async def execute(self, **params: Any) -> ToolResult:
        target = str(params.get("target", ""))
        mode = str(params.get("mode", "systemctl"))
        if target not in _SAFE_DEPLOY_TARGETS:
            return ToolResult(
                tool_name="deploy",
                content=(
                    f"[deploy: unknown target {target!r}; "
                    f"valid targets: {sorted(_SAFE_DEPLOY_TARGETS)}]"
                ),
            )
        svc = f"hiveos-{target}"
        if mode == "docker":
            container = str(params.get("container", "") or svc)
            raw = await self._run_cmd(f"docker restart {container}")
        elif mode == "ssh":
            if not self._ssh_host:
                return ToolResult(tool_name="deploy", success=False,
                                  content="[deploy: HIVE_DEPLOY_SSH_HOST not configured]")
            key_opt = f"-i {self._ssh_key} " if self._ssh_key else ""
            ssh_cmd = (
                f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "
                f"{key_opt}{self._ssh_host} "
                f"'systemctl restart {svc}.service'"
            )
            raw = await self._run_cmd(ssh_cmd)
        else:
            # default: systemctl (local)
            raw = await self._run_cmd(f"systemctl restart {svc}.service")
        # Prefix with service name for observability (existing tests depend on this).
        return ToolResult(tool_name="deploy", success=raw.success,
                          content=f"{svc}: {raw.content}")


class ExternalMessage(_Gated):
    _name, _desc = "external_message", "Send an external message via telegram/email/slack/discord (requires approval)."

    def __init__(self, telegram_token: str = "",
                 smtp_host: str = "", smtp_port: int = 587,
                 smtp_user: str = "", smtp_pass: str = "", smtp_to: str = "",
                 slack_webhook: str = "", discord_webhook: str = "") -> None:
        super().__init__()
        self._telegram_token = telegram_token
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._smtp_user = smtp_user
        self._smtp_pass = smtp_pass
        self._smtp_to = smtp_to
        self._slack_webhook = slack_webhook
        self._discord_webhook = discord_webhook

    async def execute(self, **params: Any) -> ToolResult:
        to = str(params.get("to", ""))
        body = str(params.get("body", ""))
        channel = str(params.get("channel", "telegram")).lower()

        if channel == "email":
            return await self._send_email(body)
        if channel == "slack":
            return await self._send_slack(body)
        if channel == "discord":
            return await self._send_discord(body)
        # default: telegram
        return await self._send_telegram(to, body)

    async def _send_telegram(self, to: str, body: str) -> ToolResult:
        if not self._telegram_token:
            return ToolResult(tool_name="external_message",
                              content="[external_message: TELEGRAM_BOT_TOKEN not set]")
        from hive.gateway.channels.base import OutgoingMessage
        from hive.gateway.channels.telegram import TelegramChannel
        ch = TelegramChannel(self._telegram_token)
        try:
            result = await ch.send(OutgoingMessage(chat_id=to, text=body))
        finally:
            await ch.aclose()
        if result.ok:
            return ToolResult(tool_name="external_message",
                              content=f"sent to {to} (msg_id={result.message_id})")
        return ToolResult(tool_name="external_message",
                          content=f"[external_message: send failed — {result.error}]")

    async def _send_email(self, body: str) -> ToolResult:
        if not all([self._smtp_host, self._smtp_user, self._smtp_pass, self._smtp_to]):
            return ToolResult(tool_name="external_message", success=False,
                              content="[email: set HIVE_SMTP_HOST/USER/PASS/TO]")
        import asyncio
        import email.mime.text as _mime
        import smtplib

        msg = _mime.MIMEText(body)
        msg["Subject"] = f"[Hive] {body[:60]}"
        msg["From"] = self._smtp_user
        msg["To"] = self._smtp_to

        def _send() -> None:
            with smtplib.SMTP(self._smtp_host, self._smtp_port) as s:
                s.starttls()
                s.login(self._smtp_user, self._smtp_pass)
                s.sendmail(self._smtp_user, [self._smtp_to], msg.as_string())

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _send)
        return ToolResult(tool_name="external_message",
                          content=f"Email sent to {self._smtp_to}", cost_usd=0.0)

    async def _send_slack(self, body: str) -> ToolResult:
        if not self._slack_webhook:
            return ToolResult(tool_name="external_message", success=False,
                              content="[slack: set HIVE_SLACK_WEBHOOK]")
        import asyncio
        import json as _json
        import urllib.request

        payload = _json.dumps({"text": body}).encode()

        def _post() -> int:
            req = urllib.request.Request(
                self._slack_webhook, data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
                return r.status

        loop = asyncio.get_event_loop()
        status = await loop.run_in_executor(None, _post)
        ok = status == 200
        return ToolResult(tool_name="external_message",
                          content=f"Slack: {'ok' if ok else 'failed'}", cost_usd=0.0,
                          success=ok)

    async def _send_discord(self, body: str) -> ToolResult:
        if not self._discord_webhook:
            return ToolResult(tool_name="external_message", success=False,
                              content="[discord: set HIVE_DISCORD_WEBHOOK]")
        import asyncio
        import json as _json
        import urllib.request

        # Discord requires {"content": ...}; max 2000 chars per message
        payload = _json.dumps({"content": body[:2000]}).encode()

        def _post() -> int:
            req = urllib.request.Request(
                self._discord_webhook, data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
                return r.status

        loop = asyncio.get_event_loop()
        status = await loop.run_in_executor(None, _post)
        # Discord returns 204 No Content on success
        ok = status in (200, 204)
        return ToolResult(tool_name="external_message",
                          content=f"Discord: {'ok' if ok else f'failed (status {status})'}", cost_usd=0.0,
                          success=ok)


class DiscoverTool(BaseTool):
    """Discovery-first (HARD SOUL rule): search official sources for an existing
    skill/MCP/library BEFORE building. Read-only (network search), so not gated.
    Caches via memory when the provider supports recall/learn (LocalMemoryProvider)."""

    spec = ToolSpec(
        name="discover",
        description="Search official sources (MCP registry, GitHub) for an existing "
                    "skill/MCP server/library before building; results cached to memory.",
        parameters={"type": "object", "properties": {"need": {"type": "string"}},
                    "required": ["need"]}, category="discovery")

    def __init__(self, memory: Any = None, github_token: str = "",
                 enable_security_audit: bool = True) -> None:
        # Only use memory for caching if it duck-types discovery.MemoryLike.
        self._memory = memory if (hasattr(memory, "recall") and hasattr(memory, "learn")) else None
        self._token = github_token
        self._enable_security_audit = enable_security_audit

    async def execute(self, **params: Any) -> ToolResult:
        import json
        need = str(params.get("need", ""))
        local_hits = _introspect.search(need, k=5)
        if local_hits and local_hits[0]["score"] >= _LOCAL_SCORE_THRESHOLD:
            result = {"need": need, "cached": False,
                      "source": "ast",
                      "candidates": _introspect.format_for_discover(local_hits)}
            return ToolResult(tool_name="discover", content=json.dumps(result)[:8_000])
        security_delegate = None
        if self._enable_security_audit:
            async def _sec(task: str) -> str:
                from hive.agents.delegate import delegate_named  # local import, DAG OK
                results = await delegate_named([task], "security-reviewer")
                return results[0].content if results else "[no result]"
            security_delegate = _sec
        result = await _discovery.discover(
            need, memory=self._memory, github_token=self._token,
            security_delegate=security_delegate)
        result["source"] = "web"
        return ToolResult(tool_name="discover", content=json.dumps(result)[:8_000])


class DelegateToSpecialist(BaseTool):
    """Delegate a task to a named specialist sub-agent (SOUL.md: Hive is the CEO).

    Available agents: researcher, coder, reviewer, memory-keeper, security-reviewer.
    The specialist runs in an isolated leaf context — it cannot re-delegate."""

    spec = ToolSpec(
        name="delegate_to_specialist",
        description="Delegate a task to a named specialist sub-agent. "
                    "Use before doing deep work yourself. "
                    "Available agents: researcher, coder, reviewer, memory-keeper, security-reviewer.",
        parameters={
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "enum": ["researcher", "coder", "reviewer", "memory-keeper", "security-reviewer"],
                    "description": "Specialist to delegate to.",
                },
                "task": {
                    "type": "string",
                    "description": "Full task description for the specialist.",
                },
            },
            "required": ["agent", "task"],
        },
        category="agents",
    )

    def __init__(self, *, bus: Any = None) -> None:
        self._bus = bus

    async def execute(self, **params: Any) -> ToolResult:
        from hive.agents.delegate import delegate_via_envelope
        agent = str(params.get("agent", ""))
        task = str(params.get("task", ""))
        # TODO: session_id is deferred — needs orchestrator-level plumbing so tool
        # calls carry the parent chat session_id; delegate_via_envelope already
        # supports session_id for callers that have one (see tests/test_a2a.py).
        try:
            result = await delegate_via_envelope(task, agent, bus=self._bus)
            content = result.content if result else "[no result]"
        except KeyError as exc:
            content = f"[delegate error: {exc}]"
        except Exception as exc:  # noqa: BLE001
            content = f"[delegate error: {type(exc).__name__}: {exc}]"
        return ToolResult(tool_name="delegate_to_specialist", content=content[:12_000])


class ObsidianRead(BaseTool):
    """Read a note from the Obsidian vault by kind + topic."""
    spec = ToolSpec(
        name="obsidian_read",
        description="Read a note from the Obsidian vault by kind and topic.",
        parameters={"type": "object", "properties": {
            "kind": {"type": "string", "description": "Note kind (e.g. skill, fact, research)."},
            "topic": {"type": "string", "description": "Note topic / filename stem."},
        }, "required": ["kind", "topic"]},
        category="memory",
    )

    def __init__(self, vault_path: str | Path = "") -> None:
        self._vault_path = Path(vault_path) if vault_path else Path("vault")

    async def execute(self, **params: Any) -> ToolResult:
        from hive.memory.vault import ObsidianVault
        vault = ObsidianVault(self._vault_path)
        body = vault.read(str(params.get("kind", "")), str(params.get("topic", "")))
        if body is None:
            return ToolResult(tool_name="obsidian_read", content="[note not found]", success=False)
        return ToolResult(tool_name="obsidian_read", content=body[:12_000])


class ObsidianSearch(BaseTool):
    """Search across all Obsidian vault notes by keyword query."""
    spec = ToolSpec(
        name="obsidian_search",
        description="Full-text search across Obsidian vault notes. Returns ranked snippets.",
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "Search query (space-separated keywords)."},
            "limit": {"type": "integer", "description": "Max results (default 10).", "default": 10},
        }, "required": ["query"]},
        category="memory",
    )

    def __init__(self, vault_path: str | Path = "") -> None:
        self._vault_path = Path(vault_path) if vault_path else Path("vault")

    async def execute(self, **params: Any) -> ToolResult:
        import json

        from hive.memory.vault import ObsidianVault
        vault = ObsidianVault(self._vault_path)
        results = vault.search(str(params.get("query", "")),
                               limit=int(params.get("limit", 10)))
        return ToolResult(tool_name="obsidian_search",
                          content=json.dumps(results) if results else "[no results]")


class ObsidianList(BaseTool):
    """List notes in the Obsidian vault, optionally filtered by kind."""
    spec = ToolSpec(
        name="obsidian_list",
        description="List notes in the Obsidian vault. Optionally filter by kind.",
        parameters={"type": "object", "properties": {
            "kind": {"type": "string", "description": "Filter by kind (e.g. skill, fact). Omit for all."},
        }},
        category="memory",
    )

    def __init__(self, vault_path: str | Path = "") -> None:
        self._vault_path = Path(vault_path) if vault_path else Path("vault")

    async def execute(self, **params: Any) -> ToolResult:
        import json

        from hive.memory.vault import ObsidianVault
        vault = ObsidianVault(self._vault_path)
        kind = params.get("kind") or None
        notes = vault.list_notes(kind=str(kind) if kind else None)
        if not notes:
            return ToolResult(tool_name="obsidian_list", content="[vault is empty]")
        # Return lightweight list (no full path, just kind+topic+modified)
        summary = [{"kind": n["kind"], "topic": n["topic"]} for n in notes[:100]]
        return ToolResult(tool_name="obsidian_list",
                          content=f"{len(notes)} notes\n" + json.dumps(summary))


_GH_API = "https://api.github.com"


class _GitHubBase(BaseTool):
    """Base for GitHub API tools — injects token + owner/repo."""

    def __init__(self, token: str = "", owner: str = "", repo: str = "") -> None:
        self._token = token
        self._owner = owner
        self._repo = repo

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _available(self) -> bool:
        return bool(self._token and self._owner and self._repo)

    async def _get(self, path: str, params: dict | None = None) -> Any:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{_GH_API}{path}", headers=self._headers(), params=params or {})
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, body: dict) -> Any:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{_GH_API}{path}", headers=self._headers(), json=body)
            r.raise_for_status()
            return r.json()


class GitHubListPRs(_GitHubBase):
    spec = ToolSpec(
        name="github_list_prs",
        description="List open pull requests in the configured GitHub repository.",
        parameters={"type": "object", "properties": {
            "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
            "limit": {"type": "integer", "description": "Max PRs to return (default 20).", "default": 20},
        }},
        category="github",
    )

    async def execute(self, **params: Any) -> ToolResult:
        import json
        if not self._available():
            return ToolResult(tool_name="github_list_prs", success=False,
                              content="[github_list_prs: set HIVE_GITHUB_TOKEN/OWNER/REPO]")
        state = str(params.get("state", "open"))
        limit = int(params.get("limit", 20))
        try:
            prs = await self._get(f"/repos/{self._owner}/{self._repo}/pulls",
                                  {"state": state, "per_page": min(limit, 100)})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_name="github_list_prs", success=False,
                              content=f"[github error: {exc}]")
        summary = [{"number": pr["number"], "title": pr["title"],
                    "state": pr["state"], "draft": pr.get("draft", False),
                    "author": pr["user"]["login"], "url": pr["html_url"]}
                   for pr in prs[:limit]]
        return ToolResult(tool_name="github_list_prs",
                          content=f"{len(summary)} PR(s)\n" + json.dumps(summary, indent=2))


class GitHubGetPR(_GitHubBase):
    spec = ToolSpec(
        name="github_get_pr",
        description="Get details of a specific pull request including CI status.",
        parameters={"type": "object", "properties": {
            "number": {"type": "integer", "description": "PR number."},
        }, "required": ["number"]},
        category="github",
    )

    async def execute(self, **params: Any) -> ToolResult:
        import json
        if not self._available():
            return ToolResult(tool_name="github_get_pr", success=False,
                              content="[github_get_pr: set HIVE_GITHUB_TOKEN/OWNER/REPO]")
        number = int(params.get("number", 0))
        try:
            pr = await self._get(f"/repos/{self._owner}/{self._repo}/pulls/{number}")
            checks = await self._get(
                f"/repos/{self._owner}/{self._repo}/commits/{pr['head']['sha']}/check-runs")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_name="github_get_pr", success=False,
                              content=f"[github error: {exc}]")
        check_summary = [{"name": c["name"], "status": c["status"],
                          "conclusion": c.get("conclusion")}
                         for c in checks.get("check_runs", [])]
        result = {
            "number": pr["number"], "title": pr["title"],
            "state": pr["state"], "draft": pr.get("draft", False),
            "author": pr["user"]["login"], "body": (pr.get("body") or "")[:500],
            "url": pr["html_url"], "diff_url": pr["diff_url"],
            "ci_checks": check_summary,
        }
        return ToolResult(tool_name="github_get_pr", content=json.dumps(result, indent=2))


class GitHubListCommits(_GitHubBase):
    spec = ToolSpec(
        name="github_list_commits",
        description="List recent commits in the configured GitHub repository.",
        parameters={"type": "object", "properties": {
            "branch": {"type": "string", "description": "Branch name (default: default branch)."},
            "limit": {"type": "integer", "description": "Max commits to return (default 10).", "default": 10},
        }},
        category="github",
    )

    async def execute(self, **params: Any) -> ToolResult:
        import json
        if not self._available():
            return ToolResult(tool_name="github_list_commits", success=False,
                              content="[github_list_commits: set HIVE_GITHUB_TOKEN/OWNER/REPO]")
        limit = int(params.get("limit", 10))
        api_params: dict = {"per_page": min(limit, 100)}
        if params.get("branch"):
            api_params["sha"] = str(params["branch"])
        try:
            commits = await self._get(f"/repos/{self._owner}/{self._repo}/commits", api_params)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_name="github_list_commits", success=False,
                              content=f"[github error: {exc}]")
        summary = [{"sha": c["sha"][:8], "message": c["commit"]["message"].split("\n")[0][:80],
                    "author": c["commit"]["author"]["name"],
                    "date": c["commit"]["author"]["date"]}
                   for c in commits[:limit]]
        return ToolResult(tool_name="github_list_commits",
                          content=json.dumps(summary, indent=2))


class GitHubCreateIssue(_GitHubBase, _Gated):
    _name = "github_create_issue"
    _desc = "Create a GitHub issue in the configured repository (requires approval)."

    def __init__(self, token: str = "", owner: str = "", repo: str = "") -> None:
        _Gated.__init__(self)
        self.spec = ToolSpec(
            name=self._name, description=self._desc,
            parameters={"type": "object", "properties": {
                "title": {"type": "string", "description": "Issue title."},
                "body": {"type": "string", "description": "Issue body (markdown)."},
                "labels": {"type": "array", "items": {"type": "string"},
                           "description": "Labels to apply."},
            }, "required": ["title"]},
            dangerous=True, category="github",
        )
        self._token = token
        self._owner = owner
        self._repo = repo

    async def execute(self, **params: Any) -> ToolResult:
        if not self._available():
            return ToolResult(tool_name="github_create_issue", success=False,
                              content="[github_create_issue: set HIVE_GITHUB_TOKEN/OWNER/REPO]")
        payload: dict = {"title": str(params.get("title", ""))}
        if params.get("body"):
            payload["body"] = str(params["body"])
        if params.get("labels"):
            payload["labels"] = list(params["labels"])
        try:
            issue = await self._post(f"/repos/{self._owner}/{self._repo}/issues", payload)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_name="github_create_issue", success=False,
                              content=f"[github error: {exc}]")
        return ToolResult(tool_name="github_create_issue",
                          content=f"Created issue #{issue['number']}: {issue['html_url']}")


class QueryMemory(BaseTool):
    """Mid-turn reactive memory search — call when you need facts not in the system prompt."""
    spec = ToolSpec(
        name="query_memory",
        description=(
            "Search Hive's memory for stored facts, skills, or past episodes. "
            "Use mid-turn when you need specific information that was not in the system prompt."
        ),
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "Search terms or question"},
            "limit": {"type": "integer", "default": 5, "description": "Max results to return"},
        }, "required": ["query"]},
        category="memory",
    )

    def __init__(self, memory: Any = None) -> None:
        self._memory = memory

    def available(self) -> bool:
        return self._memory is not None

    async def execute(self, **params: Any) -> ToolResult:
        import json
        query = str(params.get("query", ""))
        limit = int(params.get("limit", 5))
        if self._memory is None:
            return ToolResult(tool_name="query_memory", success=False,
                              content="[query_memory: no memory provider configured]")
        try:
            results = self._memory.recall(query, limit=limit)
            content = json.dumps(results, indent=2) if results else "[no results]"
        except Exception as exc:  # noqa: BLE001
            content = f"[query_memory error: {exc}]"
        return ToolResult(tool_name="query_memory", success=True, content=content)


class CreateTask(BaseTool):
    """Schedule a tool call to run on the next heartbeat tick without blocking this turn."""
    spec = ToolSpec(
        name="create_task",
        description=(
            "Schedule a tool call to run on the next heartbeat tick. "
            "Use to defer work that shouldn't block the current conversation turn."
        ),
        parameters={"type": "object", "properties": {
            "tool": {"type": "string", "description": "Name of the tool to call"},
            "args": {"type": "object", "description": "Arguments for the tool", "default": {}},
            "reason": {"type": "string", "description": "Why this task needs to be done"},
            "delay_seconds": {"type": "integer", "default": 0,
                              "description": "Seconds from now to schedule (0 = next tick)"},
        }, "required": ["tool", "reason"]},
        category="autonomy",
    )

    def __init__(self, task_board: Any = None) -> None:
        self._board = task_board

    def available(self) -> bool:
        return self._board is not None

    async def execute(self, **params: Any) -> ToolResult:
        import time
        tool_name = str(params.get("tool", ""))
        reason = str(params.get("reason", ""))
        args = params.get("args") or {}
        delay = int(params.get("delay_seconds", 0))
        if self._board is None:
            return ToolResult(tool_name="create_task", success=False,
                              content="[create_task: no task board configured]")
        if not tool_name:
            return ToolResult(tool_name="create_task", success=False,
                              content="[create_task: 'tool' parameter is required]")
        scheduled = time.time() + max(0, delay)
        try:
            task_id = self._board.enqueue(
                "tool", {"tool": tool_name, "args": args if isinstance(args, dict) else {}, "reason": reason},
                scheduled_for=scheduled, source="agent",
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_name="create_task", success=False,
                              content=f"[create_task error: {exc}]")
        return ToolResult(tool_name="create_task", success=True,
                          content=f"Task {task_id} scheduled: {tool_name} — {reason}")


class HiveStatus(BaseTool):
    """Mid-turn self-introspection: let the agent check its own operational health."""

    spec = ToolSpec(
        name="hive_status",
        description=(
            "Check Hive's operational health: budget, tool error rate, task queue stats, "
            "and self-modification success rate. Use when you need to self-regulate or "
            "decide whether to trigger self-improvement."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        category="autonomy",
    )

    def __init__(self, hive: Any = None) -> None:
        self._hive = hive

    def available(self) -> bool:
        return self._hive is not None

    async def execute(self, **params: Any) -> ToolResult:
        if not self._hive:
            return ToolResult(tool_name="hive_status", success=False,
                              content="[hive_status: not wired]")
        import json as _json
        parts: list[str] = []
        try:
            snap = self._hive.budgeter.snapshot()
            parts.append(f"budget: {_json.dumps(snap)}")
        except Exception:  # noqa: BLE001
            pass
        try:
            rate = self._hive.audit_log.error_rate(24.0)
            parts.append(f"tool error rate (24h): {rate:.1%}")
        except Exception:  # noqa: BLE001
            pass
        try:
            stats = self._hive.task_board.statistics()
            by_state = stats.get("by_state", {})
            pending = by_state.get("pending", {}).get("count", 0)
            failed = by_state.get("failed", {}).get("count", 0)
            parts.append(f"tasks: {pending} pending, {failed} failed")
        except Exception:  # noqa: BLE001
            pass
        try:
            sm_rate = self._hive.self_modifier.success_rate()
            parts.append(f"self-mod success rate: {sm_rate:.1%}")
        except Exception:  # noqa: BLE001
            pass
        content = "\n".join(parts) or "[no status data available]"
        return ToolResult(tool_name="hive_status", success=True, content=content)


BUILTIN_TOOLS: tuple[type[BaseTool], ...] = (
    ReadFile, WriteFile, DeleteFile, Shell, WebGet, SpendMoney, Deploy,
    DelegateToSpecialist,
)


def register_builtins(registry: type[ToolRegistry] = ToolRegistry, *,
                      memory: Any = None, task_board: Any = None,
                      hive: Any = None,
                      events: Any = None,
                      github_token: str = "",
                      github_owner: str = "", github_repo: str = "",
                      telegram_token: str = "",
                      smtp_host: str = "", smtp_port: int = 587,
                      smtp_user: str = "", smtp_pass: str = "", smtp_to: str = "",
                      slack_webhook: str = "", discord_webhook: str = "",
                      vault_path: str | Path = "",
                      shell_provider: ShellProvider | None = None,
                      deploy_ssh_host: str = "", deploy_ssh_key: str = "",
                      stripe_secret_key: str = "", stripe_customer_id: str = "") -> dict[str, BaseTool]:
    """Instantiate + register every builtin. Returns the name->tool snapshot.
    `memory` enables QueryMemory + discovery-first caching.
    `task_board` enables CreateTask (agent-scheduled async work).
    `hive` enables HiveStatus (self-introspection mid-turn).
    `events` wires DelegateToSpecialist to the A2A event bus so delegations emit
        a2a.call.* events for the Kanban board / dashboard WebSocket.
    `telegram_token` enables ExternalMessage to send Telegram messages.
    SMTP params enable email sending; `slack_webhook` enables Slack messages.
    `discord_webhook` enables Discord notifications.
    `vault_path` enables Obsidian vault read/search tools.
    `shell_provider` overrides the default LocalShellProvider (e.g. DockerShellProvider).
    `deploy_ssh_host`/`deploy_ssh_key` enable SSH deploy mode.
    `stripe_secret_key`/`stripe_customer_id` enable Stripe payment backend."""
    for tool_cls in BUILTIN_TOOLS:
        if tool_cls is Shell and shell_provider is not None:
            registry.add(Shell(provider=shell_provider))
        elif tool_cls is Deploy:
            registry.add(Deploy(ssh_host=deploy_ssh_host, ssh_key=deploy_ssh_key))
        elif tool_cls is SpendMoney:
            registry.add(SpendMoney(stripe_key=stripe_secret_key, stripe_customer=stripe_customer_id))
        elif tool_cls is DelegateToSpecialist:
            registry.add(DelegateToSpecialist(bus=events))
        else:
            registry.add(tool_cls())
    registry.add(HiveStatus(hive=hive))
    registry.add(ExternalMessage(
        telegram_token=telegram_token,
        smtp_host=smtp_host, smtp_port=smtp_port,
        smtp_user=smtp_user, smtp_pass=smtp_pass, smtp_to=smtp_to,
        slack_webhook=slack_webhook, discord_webhook=discord_webhook,
    ))
    registry.add(DiscoverTool(memory=memory, github_token=github_token,
                              enable_security_audit=True))
    registry.add(QueryMemory(memory=memory))
    registry.add(CreateTask(task_board=task_board))
    registry.add(ObsidianRead(vault_path=vault_path))
    registry.add(ObsidianSearch(vault_path=vault_path))
    registry.add(ObsidianList(vault_path=vault_path))
    # GitHub tools — registered only when token is configured
    if github_token:
        _gh_owner = github_owner
        _gh_repo = github_repo
        registry.add(GitHubListPRs(token=github_token, owner=_gh_owner, repo=_gh_repo))
        registry.add(GitHubGetPR(token=github_token, owner=_gh_owner, repo=_gh_repo))
        registry.add(GitHubListCommits(token=github_token, owner=_gh_owner, repo=_gh_repo))
        registry.add(GitHubCreateIssue(token=github_token, owner=_gh_owner, repo=_gh_repo))
    return registry.snapshot()
