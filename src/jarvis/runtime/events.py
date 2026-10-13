from __future__ import annotations

import asyncio
import inspect
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    MIC_START = "MIC_START"
    ASR_START = "ASR_START"
    ASR_FIRST = "ASR_FIRST"
    ASR_FINAL = "ASR_FINAL"
    LLM_START = "LLM_START"
    LLM_FIRST_TOKEN = "LLM_FIRST_TOKEN"
    TTS_START = "TTS_START"
    TTS_FIRST_AUDIO = "TTS_FIRST_AUDIO"
    PLAYBACK_START = "PLAYBACK_START"
    VAD_START = "VAD_START"
    INTERRUPT = "INTERRUPT"
    VOICE_LATENCY = "VOICE_LATENCY"
    STATE_CHANGED = "STATE_CHANGED"
    TASK_START = "TASK_START"
    PLAN_CREATED = "PLAN_CREATED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_STEP = "EXECUTION_STEP"
    EXECUTION_OBSERVATION = "EXECUTION_OBSERVATION"
    EXECUTION_VERIFIED = "EXECUTION_VERIFIED"
    EXECUTION_RECOVERED = "EXECUTION_RECOVERED"
    EXECUTION_COMPLETE = "EXECUTION_COMPLETE"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    EXECUTION_CANCELLED = "EXECUTION_CANCELLED"
    TOKEN_BUDGET = "TOKEN_BUDGET"
    TOOL_REQUEST = "TOOL_REQUEST"
    PERMISSION_REQUEST = "PERMISSION_REQUEST"
    PERMISSION_GRANTED = "PERMISSION_GRANTED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    PERMISSION_RESOLVED = "PERMISSION_RESOLVED"
    TOOL_START = "TOOL_START"
    TOOL_RESULT = "TOOL_RESULT"
    TOOL_ERROR = "TOOL_ERROR"
    MCP_CONNECTED = "MCP_CONNECTED"
    MCP_RECONNECTED = "MCP_RECONNECTED"
    MCP_TOOL_CALL = "MCP_TOOL_CALL"
    MCP_TOOL_RESULT = "MCP_TOOL_RESULT"
    MCP_ERROR = "MCP_ERROR"
    BACKGROUND_TASK_STARTED = "BACKGROUND_TASK_STARTED"
    BACKGROUND_TASK_COMPLETED = "BACKGROUND_TASK_COMPLETED"
    BACKGROUND_TASK_FAILED = "BACKGROUND_TASK_FAILED"
    BACKGROUND_TASK_CANCELLED = "BACKGROUND_TASK_CANCELLED"
    REMINDER_DUE = "REMINDER_DUE"
    PROACTIVE_NOTIFICATION = "PROACTIVE_NOTIFICATION"
    SYSTEM_ALERT = "SYSTEM_ALERT"
    TASK_COMPLETE = "TASK_COMPLETE"
    TASK_CANCELLED = "TASK_CANCELLED"
    TASK_FAILED = "TASK_FAILED"


@dataclass(frozen=True)
class RuntimeEvent:
    type: EventType | str
    data: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    request_id: str | None = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": str(self.type),
            "data": self.data,
            "task_id": self.task_id,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
        }


EventHandler = Callable[[RuntimeEvent], Any | Awaitable[Any]]


class EventBus:
    """Small in-process event bus with bounded replay history."""

    def __init__(self, history_size: int = 256) -> None:
        self._handlers: list[tuple[set[str] | None, EventHandler]] = []
        self._history: deque[RuntimeEvent] = deque(maxlen=history_size)

    def subscribe(
        self,
        handler: EventHandler,
        event_types: set[EventType | str] | None = None,
    ) -> Callable[[], None]:
        normalized = {str(item) for item in event_types} if event_types else None
        entry = (normalized, handler)
        self._handlers.append(entry)

        def unsubscribe() -> None:
            try:
                self._handlers.remove(entry)
            except ValueError:
                pass

        return unsubscribe

    def history(self) -> list[RuntimeEvent]:
        return list(self._history)

    def emit(self, event: RuntimeEvent) -> None:
        self._history.append(event)
        event_type = str(event.type)
        for accepted_types, handler in list(self._handlers):
            if accepted_types is not None and event_type not in accepted_types:
                continue
            result = handler(event)
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()

    async def emit_async(self, event: RuntimeEvent) -> None:
        self._history.append(event)
        event_type = str(event.type)
        for accepted_types, handler in list(self._handlers):
            if accepted_types is not None and event_type not in accepted_types:
                continue
            result = handler(event)
            if inspect.isawaitable(result):
                await result

    @staticmethod
    def queue_handler(queue: asyncio.Queue[RuntimeEvent]) -> EventHandler:
        def handler(event: RuntimeEvent) -> None:
            queue.put_nowait(event)

        return handler
