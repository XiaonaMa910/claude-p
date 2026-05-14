from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class SDKMessage:
    """Base message wrapper returned by the Python SDK."""

    type: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class SystemMessage(SDKMessage):
    subtype: str | None = None


@dataclass(frozen=True)
class StreamEventMessage(SDKMessage):
    event: dict[str, Any] | None = None


@dataclass(frozen=True)
class AssistantMessage(SDKMessage):
    text: str


@dataclass(frozen=True)
class ResultMessage(SDKMessage):
    result: str
    is_error: bool
    session_id: str | None = None
    terminal_reason: str | None = None


MessageKind = Literal["system", "stream_event", "assistant", "result", "raw"]

