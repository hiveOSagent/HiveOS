"""
executor.py — tool dispatch with approval-gate guardrails + audit.

ADAPT of OpenJarvis ToolExecutor + Hermes tool_executor/file_safety (SYNTHESIS
Part B). One choke point for every tool call:

  unknown -> ERROR
  dangerous (spec flag OR gate.is_dangerous) -> route to the PROTECTED gate,
      return PENDING with an approval id, DO NOT execute
  otherwise -> execute, capturing failures as ERROR

The danger firewall is the canonical Core/approval_gate.py (via core.approval) —
never re-implemented here. Audit + EventBus are injected so the storage sink
(observability, P8) stays decoupled. A closed ToolDispatch makes the three
outcomes unrepresentable-as-anything-else (OpenClaw: make impossible states
unrepresentable).
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from hive.core import approval
from hive.core.events import EventBus, EventType
from hive.core.types import ToolResult
from hive.tools.base import BaseTool
from hive.tools.file_safety import check_path


class _GateLike(Protocol):
    def is_dangerous(self, name: str, args: dict) -> bool: ...
    def request(self, name: str, args: dict, reason: str) -> object: ...

log = logging.getLogger("hive.tools.executor")

AuditSink = Callable[[dict[str, Any]], None]


class DispatchStatus(str, enum.Enum):
    OK = "ok"
    PENDING = "pending_approval"
    ERROR = "error"


@dataclass(slots=True)
class ToolDispatch:
    status: DispatchStatus
    result: ToolResult | None = None
    approval_id: str | None = None
    error: str | None = None


class ToolExecutor:
    def __init__(
        self,
        tools: Mapping[str, BaseTool],
        *,
        gate: _GateLike | None = None,
        audit: AuditSink | None = None,
        events: EventBus | None = None,
        timeout: float | None = 60.0,
    ) -> None:
        self._tools = dict(tools)
        self._gate = gate or approval.gate
        self._audit = audit
        self._events = events
        self._timeout = timeout  # max seconds per tool call; None = no limit

    def add_tool(self, tool: BaseTool) -> None:
        """Register a tool after construction (e.g. MCP tools loaded at startup)."""
        self._tools[tool.spec.name] = tool

    def remove_tool(self, name: str) -> bool:
        """Deregister a tool by name. Returns False if not found."""
        return self._tools.pop(name, None) is not None

    def list_tools(self) -> list[str]:
        """Return sorted list of registered tool names."""
        return sorted(self._tools.keys())

    def has_tool(self, name: str) -> bool:
        """Return True if a tool with the given name is registered."""
        return name in self._tools

    def stats(self) -> dict:
        """Return a summary of the registered tool registry.

        Includes total count, available/unavailable split, dangerous tool names,
        and per-category breakdown."""
        tools = list(self._tools.values())
        available = [t for t in tools if t.available()]
        dangerous = [t.spec.name for t in tools if t.spec.dangerous]
        categories: dict[str, int] = {}
        for t in tools:
            cat = t.spec.category or "uncategorized"
            categories[cat] = categories.get(cat, 0) + 1
        return {
            "total": len(tools),
            "available": len(available),
            "unavailable": len(tools) - len(available),
            "dangerous_count": len(dangerous),
            "dangerous": dangerous,
            "by_category": categories,
            "timeout_seconds": self._timeout,
        }

    def tool_categories(self) -> list[str]:
        """Return sorted list of distinct tool categories registered in the executor."""
        cats = {t.spec.category or "uncategorized" for t in self._tools.values()}
        return sorted(cats)

    def dangerous_tools(self) -> list[str]:
        """Return sorted list of names of dangerous tools registered in the executor."""
        return sorted(t.spec.name for t in self._tools.values() if t.spec.dangerous)

    async def execute(self, name: str, args: dict[str, Any] | None = None,
                      *, reason: str = "") -> ToolDispatch:
        args = dict(args or {})
        tool = self._tools.get(name)
        if tool is None:
            return self._finish(name, args, ToolDispatch(
                DispatchStatus.ERROR, error=f"unknown tool: {name}"))

        # Refuse tools that report themselves unavailable (B5: missing auth/config).
        if not tool.available():
            return self._finish(name, args, ToolDispatch(
                DispatchStatus.ERROR, error=f"tool unavailable: {name}"))

        # Reject writes targeting sensitive paths before touching the gate.
        for param in ("path", "file", "filename", "destination"):
            if param in args:
                operation = "read" if name == "read_file" and param == "path" else "write"
                safety_err = check_path(str(args[param]), operation=operation)
                if safety_err:
                    return self._finish(name, args, ToolDispatch(
                        DispatchStatus.ERROR, error=safety_err))

        if tool.spec.dangerous or self._gate.is_dangerous(name, args):
            # Honor the global kill-switch: if engaged, refuse new requests rather
            # than letting them pile up in the pending queue.
            try:
                from hive.core.approval_enhancements import enhance as _enhance
                if _enhance.is_request_blocked():
                    return self._finish(name, args, ToolDispatch(
                        DispatchStatus.ERROR,
                        error="approval refused: kill-switch engaged"))
            except Exception:  # noqa: BLE001 - never let enhancements break dispatch
                pass
            approval_id = str(self._gate.request(name, args, reason))
            # Tell the enhancements layer when the request was created, so it can
            # expire it later. Best-effort — never fails the dispatch.
            try:
                from hive.core.approval_enhancements import enhance as _enhance
                _enhance.audit_request(approval_id)
            except Exception:  # noqa: BLE001
                pass
            self._emit(EventType.APPROVAL_REQUESTED, tool=name, approval_id=approval_id)
            return self._finish(name, args, ToolDispatch(
                DispatchStatus.PENDING, approval_id=approval_id))

        return self._finish(name, args, await self._run(tool, args))

    async def execute_batch(
        self, calls: list[tuple[str, dict[str, Any]]], *, reason: str = ""
    ) -> list[ToolDispatch]:
        """Execute multiple tool calls concurrently. Each result is independent."""
        import asyncio
        return list(await asyncio.gather(
            *(self.execute(name, args, reason=reason) for name, args in calls)
        ))

    async def execute_approved(self, name: str, args: dict[str, Any]) -> ToolDispatch:
        """Run a previously gated tool after the human approved it."""
        tool = self._tools.get(name)
        if tool is None:
            return self._finish(name, args, ToolDispatch(
                DispatchStatus.ERROR, error=f"unknown tool: {name}"))
        if not tool.available():
            return self._finish(name, args, ToolDispatch(
                DispatchStatus.ERROR, error=f"tool unavailable: {name}"))
        # Recheck path safety even after approval — approval proves intent, not safety.
        for param in ("path", "file", "filename", "destination"):
            if param in args:
                operation = "read" if name == "read_file" and param == "path" else "write"
                safety_err = check_path(str(args[param]), operation=operation)
                if safety_err:
                    return self._finish(name, args, ToolDispatch(
                        DispatchStatus.ERROR, error=safety_err))
        return self._finish(name, args, await self._run(tool, args), approved=True)

    async def _run(self, tool: BaseTool, args: dict[str, Any]) -> ToolDispatch:
        import asyncio
        required = tool.spec.parameters.get("required", []) if tool.spec.parameters else []
        missing = [k for k in required if k not in args]
        if missing:
            err = f"missing required argument(s): {missing}"
            log.warning("tool %s called without %s", tool.spec.name, missing)
            return ToolDispatch(DispatchStatus.ERROR, error=err)
        try:
            coro = tool.execute(**args)
            if self._timeout is not None:
                result = await asyncio.wait_for(coro, timeout=self._timeout)
            else:
                result = await coro
        except asyncio.TimeoutError:
            log.warning("tool %s timed out after %.1fs", tool.spec.name, self._timeout)
            return ToolDispatch(DispatchStatus.ERROR,
                                error=f"tool timed out after {self._timeout:.0f}s")
        except Exception as exc:  # noqa: BLE001 - surfaced as a structured error
            log.warning("tool %s failed: %s", tool.spec.name, exc)
            return ToolDispatch(DispatchStatus.ERROR, error=str(exc))
        return ToolDispatch(DispatchStatus.OK, result=result)

    def _finish(self, name: str, args: dict[str, Any], dispatch: ToolDispatch,
                *, approved: bool = False) -> ToolDispatch:
        if self._audit is not None:
            try:
                self._audit({"tool": name, "args": args, "status": dispatch.status.value,
                             "approved": approved, "error": dispatch.error})
            except Exception as exc:  # noqa: BLE001
                log.warning("audit write failed for tool %s: %s", name, exc)
        if dispatch.status is not DispatchStatus.PENDING:
            self._emit(EventType.TOOL_CALL_END, tool=name, status=dispatch.status.value)
        return dispatch

    def _emit(self, event_type: EventType, **data: object) -> None:
        if self._events is not None:
            self._events.publish(event_type, dict(data))
