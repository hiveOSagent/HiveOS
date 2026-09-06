"""
base.py — transport-only channel abstraction (M4 #sf-2).

OpenClaw rule: channels are transport-only. A channel parses an inbound platform
update into a portable MessageEvent and renders an OutgoingMessage onto the wire — it
owns NO product logic, command trees, or provider/policy decisions. The gateway wires
a channel to `hive.ask()`.

Typed presentation: surfaces speak MessageEvent/OutgoingMessage, never raw platform
payloads, so the agent core stays platform-agnostic.

DAG: gateway layer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(slots=True)
class MessageEvent:
    """A normalized inbound message from any channel."""
    text: str
    chat_id: str
    user_id: str = ""
    message_id: str = ""
    platform: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    # Gateway policy may downgrade an event before it is rejected.  This keeps
    # the trust decision explicit at the transport-to-core boundary.
    trust: Literal["trusted", "untrusted"] = "trusted"


@dataclass(slots=True)
class OutgoingMessage:
    """A portable outbound message. Channels map this to native payloads."""
    chat_id: str
    text: str
    reply_to: str | None = None
    reply_markup: dict[str, Any] | None = None


@dataclass(slots=True)
class SendResult:
    ok: bool
    message_id: str = ""
    error: str | None = None


class ChannelAdapter(ABC):
    """Transport adapter: parse inbound updates, send outbound messages."""

    name: str = "channel"

    @abstractmethod
    def parse_update(self, update: dict[str, Any]) -> MessageEvent | None:
        """Normalize a raw platform update to a MessageEvent, or None if it carries
        no actionable text (edits, reactions, joins, etc.)."""

    @abstractmethod
    async def send(self, message: OutgoingMessage) -> SendResult:
        """Deliver a portable message over the wire."""

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None
