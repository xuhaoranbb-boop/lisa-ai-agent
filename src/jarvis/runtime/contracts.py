from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from jarvis.model import ModelReply


class PermissionLevel(StrEnum):
    READ_ONLY = "READ_ONLY"
    LOW_RISK = "LOW_RISK"
    MEDIUM_RISK = "MEDIUM_RISK"
    HIGH_RISK = "HIGH_RISK"
    CRITICAL = "CRITICAL"
    NORMAL = "NORMAL"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    permission: PermissionLevel = PermissionLevel.READ_ONLY
    timeout_seconds: float = 30.0
    cancellable: bool = True
    requires_confirmation: bool = False
    capability: str = "general"
    category: str = "SYSTEM"
    risk_level: PermissionLevel = PermissionLevel.READ_ONLY
    retry_policy: dict[str, Any] = field(default_factory=dict)
    trusted_output: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class LLMProvider(Protocol):
    name: str

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply: ...

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]: ...

    async def vision(
        self,
        messages: list[dict[str, Any]],
        image: bytes,
    ) -> ModelReply: ...

    async def embedding(self, text: str) -> list[float]: ...

    async def function_call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply: ...
