from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from jarvis.runtime.cancellation import CancellationToken, OperationCancelled
from jarvis.runtime.contracts import PermissionLevel, ToolSpec
from jarvis.runtime.permissions import PermissionDenied, PermissionManager

ToolHandler = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class RuntimeTool:
    spec: ToolSpec
    handler: ToolHandler

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        permission_manager: PermissionManager | None = None,
        cancellation: CancellationToken | None = None,
    ) -> Any:
        validate_tool_arguments(self.spec.name, self.spec.parameters, arguments)
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if permission_manager is not None:
            decision = await permission_manager.require(
                self.spec.permission,
                self.spec.name,
                arguments.get("path") or arguments.get("app") or arguments.get("command"),
                metadata={
                    "capability": self.spec.capability,
                    "category": self.spec.category,
                    "requires_confirmation": self.spec.requires_confirmation,
                },
            )
            if not decision.allowed:
                raise PermissionDenied(self.spec.name, decision.reason)
        try:
            result = await self.handler(**arguments)
        except (OperationCancelled, PermissionDenied):
            raise
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        return result


class ToolRegistry:
    """Capability-aware registry shared by text, voice, and desktop agents."""

    def __init__(self) -> None:
        self._tools: dict[str, RuntimeTool] = {}

    def register(self, tool: RuntimeTool) -> None:
        self._tools[tool.spec.name] = tool

    def register_many(self, tools: list[RuntimeTool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> RuntimeTool | None:
        return self._tools.get(name)

    def all(self) -> list[RuntimeTool]:
        return list(self._tools.values())

    def schemas(
        self,
        *,
        capability: str | None = None,
        allowed_permissions: set[PermissionLevel] | None = None,
        include_handoff: bool = True,
    ) -> list[dict[str, Any]]:
        return [
            tool.spec.schema()
            for tool in self._tools.values()
            if (capability is None or tool.spec.capability == capability)
            and (
                include_handoff
                or not tool.spec.metadata.get("agent_handoff")
            )
            and (
                allowed_permissions is None
                or tool.spec.permission in allowed_permissions
            )
        ]

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        permission_manager: PermissionManager | None = None,
        cancellation: CancellationToken | None = None,
    ) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: {name}")
        return await tool.execute(
            arguments,
            permission_manager=permission_manager,
            cancellation=cancellation,
        )

    def register_toolbox(
        self,
        toolbox: Any,
        *,
        capability: str = "general",
        permission: PermissionLevel = PermissionLevel.LOW_RISK,
    ) -> None:
        runtime_tools = getattr(toolbox, "runtime_tools", None)
        if callable(runtime_tools):
            self.register_many(list(runtime_tools()))
            return
        for schema in toolbox.schemas():
            function = schema.get("function") or {}
            name = str(function.get("name") or "")
            handler = getattr(toolbox, name, None)
            if not name or not callable(handler):
                continue
            self.register(
                RuntimeTool(
                    spec=ToolSpec(
                        name=name,
                        description=str(function.get("description") or ""),
                        parameters=dict(function.get("parameters") or {}),
                        permission=_classify_tool(name, permission),
                        capability=capability,
                        category=capability.upper(),
                    ),
                    handler=handler,
                )
            )


def _classify_tool(name: str, default: PermissionLevel) -> PermissionLevel:
    read_only = {
        "get_current_time",
        "get_weather",
        "search_nearby",
        "get_system_info",
        "list_workspace",
        "read_workspace_file",
        "recall",
        "web_search",
    }
    if name in read_only:
        return PermissionLevel.READ_ONLY
    if name == "remember":
        return PermissionLevel.LOW_RISK
    if name in {"delete_file", "send_message", "app_close"}:
        return PermissionLevel.HIGH_RISK
    if name in {"payment", "transfer", "purchase"}:
        return PermissionLevel.CRITICAL
    return default


def validate_tool_arguments(
    tool_name: str,
    schema: dict[str, Any],
    arguments: dict[str, Any],
) -> None:
    required = schema.get("required") or []
    for name in required:
        if name not in arguments:
            raise ValueError(f"Tool {tool_name} is missing required argument: {name}")
    properties = schema.get("properties") or {}
    for name, value in arguments.items():
        definition = properties.get(name)
        if definition is None:
            if schema.get("additionalProperties") is False:
                raise ValueError(f"Tool {tool_name} does not accept argument: {name}")
            continue
        expected = definition.get("type")
        if expected and not _matches_json_type(value, str(expected)):
            raise ValueError(
                f"Tool {tool_name} argument {name} must be {expected}."
            )
        enum = definition.get("enum")
        if enum and value not in enum:
            raise ValueError(
                f"Tool {tool_name} argument {name} must be one of {enum}."
            )


def _matches_json_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return True
