from __future__ import annotations

import asyncio
import ctypes
import html
import math
import os
import platform
import shutil
import subprocess
import sys
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import httpx
from ddgs import DDGS

from jarvis.config import Settings
from jarvis.memory import MemoryStore
from jarvis.runtime import (
    CancellationToken,
    DesktopController,
    EventBus,
    MCPManager,
    OperationCancelled,
    PermissionLevel,
    PermissionManager,
    ReminderService,
    RuntimeTool,
    TaskScheduler,
    ToolSpec,
    VisionAnalyzer,
    permission_policy_from_settings,
)
from jarvis.runtime.tooling import validate_tool_arguments

ToolHandler = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    permission: PermissionLevel = PermissionLevel.LOW_RISK
    capability: str = "general"
    timeout_seconds: float = 30.0
    category: str = "SYSTEM"
    retry_policy: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Toolbox:
    """Tool registry shared by text and voice agents."""

    def __init__(
        self,
        settings: Settings,
        memory: MemoryStore,
        *,
        permission_manager: PermissionManager | None = None,
        scheduler: TaskScheduler | None = None,
        reminder_service: ReminderService | None = None,
        event_bus: EventBus | None = None,
        vision_provider: Any | None = None,
    ) -> None:
        self.settings = settings
        self.memory = memory
        self.permission_manager = permission_manager or PermissionManager(
            permission_policy_from_settings(settings)
        )
        self.scheduler = scheduler
        self.reminder_service = reminder_service
        self.event_bus = event_bus or EventBus()
        self.vision_provider = vision_provider
        self.desktop = DesktopController(
            permission_manager=self.permission_manager,
        )
        self._tools: dict[str, Tool] = {}
        self._register_defaults()

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def register_mcp_tools(self, manager: MCPManager) -> None:
        for mcp_tool in manager.tools():
            name = f"mcp_{_safe_tool_name(mcp_tool.server)}_{_safe_tool_name(mcp_tool.name)}"
            parameters = mcp_tool.input_schema or {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            }

            self.register(
                Tool(
                    name=name,
                    description=(
                        f"MCP tool from {mcp_tool.server}: "
                        f"{mcp_tool.description or mcp_tool.name}"
                    ),
                    parameters=parameters,
                    handler=_mcp_handler(manager, mcp_tool.server, mcp_tool.name),
                )
            )

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancellation: CancellationToken | None = None,
    ) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"Unknown tool: {name}"}
        try:
            validate_tool_arguments(tool.name, tool.parameters, arguments)
        except ValueError as exc:
            return {"ok": False, "error": f"InvalidArguments: {exc}"}
        try:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            await self.permission_manager.require(
                tool.permission,
                tool.name,
                self._resource_for(tool, arguments),
                metadata={
                    "capability": tool.capability,
                    "category": _tool_category(tool.capability, tool.name),
                    "arguments": arguments,
                },
            )
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            result = await self._run_tool_handler(
                tool,
                arguments,
                cancellation=cancellation,
            )
            return {
                "ok": True,
                "result": _json_safe_tool_output(tool.name, result),
            }
        except OperationCancelled:
            raise
        except TimeoutError:
            return {
                "ok": False,
                "error": (
                    f"TimeoutError: Tool {tool.name} exceeded "
                    f"{tool.timeout_seconds:.1f}s."
                ),
            }
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def runtime_tools(self) -> list[RuntimeTool]:
        return [
            RuntimeTool(
                spec=ToolSpec(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.parameters,
                    permission=tool.permission,
                    timeout_seconds=tool.timeout_seconds,
                    capability=tool.capability,
                    category=_tool_category(tool.capability, tool.name),
                    risk_level=tool.permission,
                    retry_policy=tool.retry_policy
                    or {
                        "max_retries": (
                            1
                            if tool.permission == PermissionLevel.READ_ONLY
                            else 0
                        ),
                        "backoff_seconds": 0.25,
                    },
                    metadata=tool.metadata or {},
                ),
                handler=tool.handler,
            )
            for tool in self._tools.values()
        ]


    @staticmethod
    async def _run_tool_handler(
        tool: Tool,
        arguments: dict[str, Any],
        *,
        cancellation: CancellationToken | None,
    ) -> Any:
        task = asyncio.create_task(
            asyncio.wait_for(
                tool.handler(**arguments),
                timeout=tool.timeout_seconds,
            )
        )
        deadline = asyncio.get_running_loop().time() + tool.timeout_seconds
        try:
            while True:
                if cancellation is not None and cancellation.cancelled:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise OperationCancelled(cancellation.reason or "Tool cancelled.")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise TimeoutError
                done, _ = await asyncio.wait({task}, timeout=min(0.05, remaining))
                if done:
                    return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    def _resource_for(tool: Tool, arguments: dict[str, Any]) -> str | None:
        for key in ("path", "application", "target", "command", "query"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    def _register_defaults(self) -> None:
        self.register(
            Tool(
                name="get_current_time",
                description="Get the current date and time in a named time zone.",
                parameters={
                    "type": "object",
                    "properties": {
                        "timezone": {
                            "type": "string",
                            "description": "IANA timezone such as Asia/Shanghai.",
                            "default": self.settings.timezone,
                        }
                    },
                    "additionalProperties": False,
                },
                handler=self.get_current_time,
            )
        )
        self.register(
            Tool(
                name="remember",
                description=(
                    "Store a durable fact, preference, project detail, or commitment "
                    "about the user."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "The fact to remember."},
                        "category": {
                            "type": "string",
                            "description": (
                                "Short category such as preference, person, project, "
                                "or commitment."
                            ),
                        },
                        "importance": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 5,
                            "description": "Durability from 1 (temporary) to 5 (critical).",
                            "default": 3,
                        },
                    },
                    "required": ["content"],
                    "additionalProperties": False,
                },
                handler=self.remember,
            )
        )
        self.register(
            Tool(
                name="recall",
                description="Search durable memory for relevant facts or preferences.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query."},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 20,
                            "default": self.settings.max_memory_results,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                handler=self.recall,
            )
        )
        self.register(
            Tool(
                name="update_memory",
                description=(
                    "Update an existing durable memory by id. Use after a preference "
                    "or project fact changes."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "integer", "minimum": 1},
                        "content": {"type": "string"},
                        "category": {"type": "string"},
                        "importance": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 5,
                        },
                        "expires_at": {
                            "type": "string",
                            "description": "ISO-8601 expiry timestamp, if any.",
                        },
                    },
                    "required": ["memory_id"],
                    "additionalProperties": False,
                },
                handler=self.update_memory,
            )
        )
        self.register(
            Tool(
                name="forget_memory",
                description="Delete a durable memory by id when it is no longer true.",
                parameters={
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "integer", "minimum": 1},
                    },
                    "required": ["memory_id"],
                    "additionalProperties": False,
                },
                handler=self.forget_memory,
            )
        )
        self.register(
            Tool(
                name="get_weather",
                description="Get current weather and a short daily forecast for a city.",
                parameters={
                    "type": "object",
                    "properties": {
                        "city": {
                            "type": "string",
                            "description": "City name, optionally with country or region.",
                        }
                    },
                    "required": ["city"],
                    "additionalProperties": False,
                },
                handler=self.get_weather,
            )
        )
        self.register(
            Tool(
                name="search_nearby",
                description=(
                    "Search nearby places around a location: cafes, restaurants, "
                    "convenience stores, pharmacies, banks, stations, hotels, and "
                    "similar. Use it for questions such as 附近的咖啡店 or 徐家汇附近的药店. "
                    "When no location is given, the configured home city is used."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": "What to look for, for example 咖啡店, 火锅, 便利店.",
                        },
                        "location": {
                            "type": "string",
                            "description": (
                                "Anchor place such as 上海人民广场. "
                                "Leave empty to use the home city."
                            ),
                        },
                        "radius": {
                            "type": "integer",
                            "description": "Search radius in meters (default 2000).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of places to return (default 8).",
                        },
                    },
                    "required": ["keyword"],
                    "additionalProperties": False,
                },
                handler=self.search_nearby,
            )
        )
        self.register(
            Tool(
                name="web_search",
                description="Search the public web and return ranked titles, URLs, and snippets.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query."},
                        "max_results": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 8,
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                handler=self.web_search,
                permission=PermissionLevel.READ_ONLY,
                capability="browser",
            )
        )
        self.register(
            Tool(
                name="web_read_page",
                description=(
                    "Fetch an HTTP or HTTPS page and extract readable title, text, and links."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "max_chars": {
                            "type": "integer",
                            "minimum": 500,
                            "maximum": 50_000,
                            "default": 8_000,
                        },
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
                handler=self.web_read_page,
                permission=PermissionLevel.READ_ONLY,
                capability="browser",
            )
        )
        self.register(
            Tool(
                name="browser_open_url",
                description="Open a URL in the default desktop browser.",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
                handler=self.browser_open_url,
                permission=PermissionLevel.LOW_RISK,
                capability="browser",
            )
        )
        self.register(
            Tool(
                name="get_system_info",
                description="Inspect the local operating system, CPU, memory, and disk space.",
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                handler=self.get_system_info,
            )
        )
        self.register(
            Tool(
                name="list_workspace",
                description=(
                    "List files and directories in a bounded path inside the configured "
                    "workspace."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path inside the workspace. Defaults to '.'.",
                            "default": ".",
                        }
                    },
                    "additionalProperties": False,
                },
                handler=self.list_workspace,
            )
        )
        self.register(
            Tool(
                name="read_workspace_file",
                description="Read a bounded UTF-8 text file from inside the configured workspace.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative file path inside the workspace.",
                        }
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                handler=self.read_workspace_file,
            )
        )
        self.register(
            Tool(
                name="computer_window_list",
                description="List visible desktop windows with process and title metadata.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=self.computer_window_list,
                permission=PermissionLevel.READ_ONLY,
                capability="computer",
            )
        )
        self.register(
            Tool(
                name="computer_screenshot",
                description="Capture the current desktop screenshot.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=self.computer_screenshot,
                permission=PermissionLevel.READ_ONLY,
                capability="computer",
            )
        )
        self.register(
            Tool(
                name="computer_ui_tree",
                description=(
                    "Inspect the Windows UI Automation tree and return visible elements."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "max_items": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 500,
                            "default": 120,
                        },
                    },
                    "additionalProperties": False,
                },
                handler=self.computer_ui_tree,
                permission=PermissionLevel.READ_ONLY,
                capability="computer",
            )
        )
        self.register(
            Tool(
                name="computer_ui_invoke",
                description=(
                    "Invoke a Windows UI Automation element by visible name or automation id. "
                    "Preferred way to activate standard buttons, menu items, and controls."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "automation_id": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                handler=self.computer_ui_invoke,
                permission=PermissionLevel.MEDIUM_RISK,
                capability="computer",
            )
        )
        self.register(
            Tool(
                name="computer_find_and_click",
                description=(
                    "Find a Windows UI Automation element and click it, using UIA "
                    "invoke first and mouse fallback only when necessary. Requires user "
                    "confirmation; use it only when computer_ui_invoke cannot activate "
                    "the control."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "automation_id": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                handler=self.computer_find_and_click,
                permission=PermissionLevel.HIGH_RISK,
                capability="computer",
            )
        )
        self.register(
            Tool(
                name="computer_open_app",
                description="Open an application on the desktop.",
                parameters={
                    "type": "object",
                    "properties": {
                        "application": {"type": "string"},
                    },
                    "required": ["application"],
                    "additionalProperties": False,
                },
                handler=self.computer_open_app,
                permission=PermissionLevel.LOW_RISK,
                capability="computer",
            )
        )
        self.register(
            Tool(
                name="computer_close_app",
                description="Close a desktop application. This is a high-risk action.",
                parameters={
                    "type": "object",
                    "properties": {
                        "application": {"type": "string"},
                    },
                    "required": ["application"],
                    "additionalProperties": False,
                },
                handler=self.computer_close_app,
                permission=PermissionLevel.HIGH_RISK,
                capability="computer",
            )
        )
        self.register(
            Tool(
                name="computer_focus_window",
                description="Focus a visible desktop window.",
                parameters={
                    "type": "object",
                    "properties": {
                        "target": {"type": "string"},
                    },
                    "required": ["target"],
                    "additionalProperties": False,
                },
                handler=self.computer_focus_window,
                permission=PermissionLevel.LOW_RISK,
                capability="computer",
            )
        )
        self.register(
            Tool(
                name="computer_keyboard_type",
                description="Type text into the focused desktop window.",
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
                handler=self.computer_keyboard_type,
                permission=PermissionLevel.MEDIUM_RISK,
                capability="computer",
            )
        )
        self.register(
            Tool(
                name="computer_keyboard_press",
                description="Press a key or key name in the focused desktop window.",
                parameters={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                    },
                    "required": ["key"],
                    "additionalProperties": False,
                },
                handler=self.computer_keyboard_press,
                permission=PermissionLevel.MEDIUM_RISK,
                capability="computer",
            )
        )
        self.register(
            Tool(
                name="computer_mouse_double_click",
                description="Double-click the desktop at optional screen coordinates.",
                parameters={
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
                handler=self.computer_mouse_double_click,
                permission=PermissionLevel.MEDIUM_RISK,
                capability="computer",
            )
        )
        self.register(
            Tool(
                name="computer_mouse_click",
                description="Click the desktop at optional screen coordinates.",
                parameters={
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
                handler=self.computer_mouse_click,
                permission=PermissionLevel.MEDIUM_RISK,
                capability="computer",
            )
        )
        self.register(
            Tool(
                name="computer_keyboard_hotkey",
                description="Press a keyboard shortcut in the focused desktop window.",
                parameters={
                    "type": "object",
                    "properties": {
                        "keys": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 2,
                        },
                    },
                    "required": ["keys"],
                    "additionalProperties": False,
                },
                handler=self.computer_keyboard_hotkey,
                permission=PermissionLevel.MEDIUM_RISK,
                capability="computer",
            )
        )
        self.register(
            Tool(
                name="computer_clipboard_read",
                description="Read the current desktop clipboard.",
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                handler=self.computer_clipboard_read,
                permission=PermissionLevel.READ_ONLY,
                capability="computer",
            )
        )
        self.register(
            Tool(
                name="computer_clipboard_write",
                description="Replace the desktop clipboard contents.",
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
                handler=self.computer_clipboard_write,
                permission=PermissionLevel.MEDIUM_RISK,
                capability="computer",
            )
        )
        self.register(
            Tool(
                name="computer_scroll",
                description="Scroll the desktop by a wheel delta.",
                parameters={
                    "type": "object",
                    "properties": {
                        "delta": {"type": "integer"},
                    },
                    "required": ["delta"],
                    "additionalProperties": False,
                },
                handler=self.computer_scroll,
                permission=PermissionLevel.NORMAL,
                capability="computer",
            )
        )
        self.register(
            Tool(
                name="run_workspace_command",
                description=(
                    "Run an allowlisted code/test command inside the configured workspace. "
                    "No shell operators are accepted."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "enum": [
                                "python",
                                "python3",
                                "pytest",
                                "ruff",
                                "node",
                                "pnpm",
                                "npm",
                                "git",
                            ],
                        },
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                        },
                        "timeout": {
                            "type": "number",
                            "minimum": 1,
                            "maximum": 120,
                            "default": 60,
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Relative working directory inside the workspace.",
                            "default": ".",
                        },
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
                handler=self.run_workspace_command,
                permission=PermissionLevel.NORMAL,
                capability="code",
                timeout_seconds=125.0,
            )
        )
        if self.reminder_service is not None:
            self.register(
                Tool(
                    name="schedule_reminder",
                    description=(
                        "Schedule a one-shot reminder that will appear in the JARVIS HUD."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "message": {"type": "string"},
                            "after_seconds": {
                                "type": "number",
                                "minimum": 1,
                                "maximum": 86_400,
                            },
                            "name": {"type": "string"},
                        },
                        "required": ["message", "after_seconds"],
                        "additionalProperties": False,
                    },
                    handler=self.schedule_reminder,
                    permission=PermissionLevel.LOW_RISK,
                    capability="proactive",
                )
            )
        if self.scheduler is not None:
            self.register(
                Tool(
                    name="run_background_command",
                    description=(
                        "Run an allowlisted workspace command in the background and notify "
                        "the HUD when it completes or fails."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "enum": [
                                    "python",
                                    "python3",
                                    "pytest",
                                    "ruff",
                                    "node",
                                    "pnpm",
                                    "npm",
                                    "git",
                                ],
                            },
                            "args": {
                                "type": "array",
                                "items": {"type": "string"},
                                "default": [],
                            },
                            "timeout": {
                                "type": "number",
                                "minimum": 1,
                                "maximum": 120,
                                "default": 60,
                            },
                            "cwd": {
                                "type": "string",
                                "default": ".",
                            },
                            "name": {"type": "string"},
                        },
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                    handler=self.run_background_command,
                    permission=PermissionLevel.NORMAL,
                    capability="tasks",
                )
            )
        if self.vision_provider is not None:
            self.register(
                Tool(
                    name="computer_analyze_screen",
                    description=(
                        "Capture the desktop and analyze it with the configured vision model. "
                        "This sends a screenshot to that model."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "prompt": {
                                "type": "string",
                                "default": (
                                    "Describe the screen and identify actionable UI elements."
                                ),
                            },
                        },
                        "additionalProperties": False,
                    },
                    handler=self.computer_analyze_screen,
                    permission=PermissionLevel.HIGH_RISK,
                    capability="vision",
                )
            )

    async def get_current_time(self, timezone: str | None = None) -> dict[str, str]:
        timezone = self._resolve_timezone(timezone or self.settings.timezone)
        now = datetime.now(ZoneInfo(timezone))
        return {
            "timezone": timezone,
            "datetime": now.isoformat(timespec="seconds"),
            "weekday": now.strftime("%A"),
        }

    @staticmethod
    def _resolve_timezone(value: str) -> str:
        aliases = {
            "beijing": "Asia/Shanghai",
            "china standard time": "Asia/Shanghai",
            "guangzhou": "Asia/Shanghai",
            "shanghai": "Asia/Shanghai",
            "shenzhen": "Asia/Shanghai",
            "tokyo": "Asia/Tokyo",
            "london": "Europe/London",
            "new york": "America/New_York",
            "los angeles": "America/Los_Angeles",
            "san francisco": "America/Los_Angeles",
            "paris": "Europe/Paris",
            "berlin": "Europe/Berlin",
            "sydney": "Australia/Sydney",
            "singapore": "Asia/Singapore",
            "hong kong": "Asia/Hong_Kong",
            "utc": "UTC",
        }
        original = value.strip()
        normalized = original.lower().replace("_", " ").replace("-", " ")
        if normalized in aliases:
            return aliases[normalized]

        try:
            ZoneInfo(original)
            return original
        except ZoneInfoNotFoundError:
            pass

        city = normalized.replace(" ", "")
        matches = [
            timezone
            for timezone in available_timezones()
            if timezone.split("/")[-1].lower().replace("_", "").replace("-", "") == city
        ]
        if len(matches) == 1:
            return matches[0]
        if normalized in {"central standard time", "china time"}:
            return "Asia/Shanghai"
        raise ValueError(
            f"Unknown timezone or city: {original}. Use an IANA timezone such as Asia/Shanghai."
        )

    async def remember(
        self,
        content: str,
        category: str = "general",
        importance: int = 3,
    ) -> dict[str, Any]:
        memory = self.memory.remember(content, category, importance=importance)
        return {
            "id": memory.id,
            "category": memory.category,
            "content": memory.content,
            "importance": memory.importance,
        }

    async def recall(self, query: str, limit: int | None = None) -> list[dict[str, str]]:
        limit = min(limit or self.settings.max_memory_results, 20)
        return [
            {
                "id": str(memory.id),
                "category": memory.category,
                "content": memory.content,
                "updated_at": memory.updated_at,
            }
            for memory in self.memory.recall_for_context(query, limit)
        ]

    async def update_memory(
        self,
        memory_id: int,
        content: str | None = None,
        category: str | None = None,
        importance: int | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        memory = self.memory.update_memory(
            memory_id,
            content=content,
            category=category,
            importance=importance,
            expires_at=expires_at,
        )
        if memory is None:
            return {"ok": False, "error": f"Memory #{memory_id} was not found."}
        return {
            "ok": True,
            "id": memory.id,
            "category": memory.category,
            "content": memory.content,
            "importance": memory.importance,
        }

    async def forget_memory(self, memory_id: int) -> dict[str, Any]:
        return {"ok": self.memory.forget(memory_id), "id": memory_id}

    async def get_weather(self, city: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            geocoding = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "zh", "format": "json"},
            )
            geocoding.raise_for_status()
            places = geocoding.json().get("results") or []
            if not places:
                raise ValueError(f"City not found: {city}")

            place = places[0]
            forecast = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "current": (
                        "temperature_2m,apparent_temperature,relative_humidity_2m,"
                        "precipitation,weather_code,wind_speed_10m"
                    ),
                    "daily": (
                        "weather_code,temperature_2m_max,temperature_2m_min,"
                        "precipitation_probability_max"
                    ),
                    "timezone": "auto",
                    "forecast_days": 3,
                },
            )
            forecast.raise_for_status()
            data = forecast.json()

        return {
            "location": {
                "name": place.get("name"),
                "admin1": place.get("admin1"),
                "country": place.get("country"),
                "latitude": place.get("latitude"),
                "longitude": place.get("longitude"),
            },
            "current": data.get("current", {}),
            "daily": data.get("daily", {}),
        }

    async def search_nearby(
        self,
        keyword: str,
        location: str | None = None,
        radius: int = 2_000,
        limit: int = 8,
    ) -> dict[str, Any]:
        keyword = (keyword or "").strip()
        if not keyword:
            raise ValueError("keyword is required.")
        radius = max(200, min(int(radius), 10_000))
        limit = max(1, min(int(limit), 20))
        anchor_name, latitude, longitude = await self._resolve_nearby_anchor(location)
        amap_key = self.settings.amap_api_key.get_secret_value().strip()
        if amap_key:
            places = await self._search_places_amap(
                keyword,
                latitude,
                longitude,
                radius,
                limit,
                amap_key,
            )
            source = "amap"
        else:
            places = await self._search_places_osm(
                keyword,
                latitude,
                longitude,
                radius,
                limit,
            )
            source = "osm"
        return {
            "ok": True,
            "source": source,
            "anchor": anchor_name,
            "count": len(places),
            "results": places,
        }

    async def _resolve_nearby_anchor(
        self,
        location: str | None,
    ) -> tuple[str, float, float]:
        amap_key = self.settings.amap_api_key.get_secret_value().strip()
        query = (location or "").strip() or self.settings.user_city.strip()
        for suffix in ("附近", "周边", "旁边", "这里", "那儿"):
            if query.endswith(suffix) and len(query) > len(suffix):
                query = query[: -len(suffix)].strip()
        if not query and amap_key:
            query = await self._amap_ip_city(amap_key)
        if not query:
            query = await self._keyless_ip_city()
        if not query:
            raise ValueError(
                "LocationRequired: 需要先知道位置——请说“XX附近”（如“上海人民广场附近”），"
                "或先配置 JARVIS_USER_CITY。"
            )
        if amap_key:
            return await self._geocode_amap_chain(
                query,
                amap_key,
                self.settings.user_city,
            )
        return await self._geocode_open_meteo(query, self.settings.user_city)

    @staticmethod
    async def _amap_ip_city(amap_key: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    "https://restapi.amap.com/v3/ip",
                    params={"key": amap_key},
                )
            data = response.json()
            if str(data.get("status")) != "1":
                return ""
            return _clean_text(data.get("city"))
        except Exception:
            return ""

    @staticmethod
    async def _keyless_ip_city() -> str:
        """Resolve the home city from the public IP (myip.ipip.net)."""
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get("https://myip.ipip.net")
            text = response.text.strip()
            # Format: 当前 IP：1.2.3.4 来自于：中国 上海 上海 联通
            if "来自于：" not in text:
                return ""
            parts = text.split("来自于：", 1)[1].split()
            if len(parts) < 2 or parts[0] != "中国":
                return ""
            return parts[1]
        except Exception:
            return ""

    @staticmethod
    async def _geocode_amap(query: str, amap_key: str) -> tuple[str, float, float]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                "https://restapi.amap.com/v3/geocode/geo",
                params={"key": amap_key, "address": query},
            )
        data = response.json()
        if str(data.get("status")) != "1":
            raise RuntimeError(f"Amap geocode failed: {data.get('info') or 'unknown'}")
        geocodes = data.get("geocodes") or []
        if not geocodes:
            raise ValueError(f"Location not found: {query}")
        item = geocodes[0]
        longitude, latitude = (
            float(part) for part in str(item.get("location")).split(",")
        )
        return (
            _clean_text(item.get("formatted_address")) or query,
            latitude,
            longitude,
        )

    @staticmethod
    async def _geocode_amap_chain(
        query: str,
        amap_key: str,
        home_city: str = "",
    ) -> tuple[str, float, float]:
        """Try home-city-prefixed candidates first, then the raw query."""
        last_miss: ValueError | None = None
        for candidate in _geocode_candidates(query, home_city):
            try:
                return await Toolbox._geocode_amap(candidate, amap_key)
            except ValueError as exc:
                last_miss = exc
                continue
        if last_miss is not None:
            raise last_miss
        raise ValueError(f"Location not found: {query}")

    @staticmethod
    async def _geocode_open_meteo(
        query: str,
        home_city: str = "",
    ) -> tuple[str, float, float]:
        candidates = _geocode_candidates(query, home_city)
        async with httpx.AsyncClient(timeout=15.0) as client:
            for candidate in candidates:
                response = await client.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={
                        "name": candidate,
                        "count": 1,
                        "language": "zh",
                        "format": "json",
                    },
                )
                places = response.json().get("results") or []
                if places:
                    place = places[0]
                    return (
                        _clean_text(place.get("name")) or candidate,
                        float(place["latitude"]),
                        float(place["longitude"]),
                    )
        raise ValueError(
            f"Location not found: {query}（免费数据源只支持城市级地名；"
            "配置高德 key 后可以识别区级位置）"
        )

    @staticmethod
    async def _search_places_amap(
        keyword: str,
        latitude: float,
        longitude: float,
        radius: int,
        limit: int,
        amap_key: str,
    ) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                "https://restapi.amap.com/v3/place/around",
                params={
                    "key": amap_key,
                    "location": f"{longitude:.6f},{latitude:.6f}",
                    "keywords": keyword,
                    "radius": radius,
                    "offset": limit,
                    "page": 1,
                    "sortrule": "distance",
                    "extensions": "all",
                },
            )
        data = response.json()
        if str(data.get("status")) != "1":
            raise RuntimeError(f"Amap search failed: {data.get('info') or 'unknown'}")
        places: list[dict[str, Any]] = []
        for poi in (data.get("pois") or [])[:limit]:
            location = str(poi.get("location") or "")
            if "," not in location:
                continue
            longitude_value, latitude_value = (
                float(part) for part in location.split(",")
            )
            entry: dict[str, Any] = {
                "name": _clean_text(poi.get("name")),
                "address": _clean_text(poi.get("address")) or "—",
                "distance_m": _to_int(poi.get("distance")),
                "type": _clean_text(poi.get("type")),
                "latitude": latitude_value,
                "longitude": longitude_value,
            }
            business = poi.get("biz_ext") or {}
            rating = _clean_text(business.get("rating"))
            if rating:
                entry["rating"] = rating
            tel = _clean_text(poi.get("tel"))
            if tel:
                entry["tel"] = tel
            places.append(entry)
        return places

    @staticmethod
    async def _search_places_osm(
        keyword: str,
        latitude: float,
        longitude: float,
        radius: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        escaped = keyword.replace("\\", "\\\\").replace('"', '\\"')
        category = _nearby_osm_filter(keyword)
        if category:
            filter_expressions = [category]
        elif len(keyword) >= 2:
            filter_expressions = [f'[name~"{escaped}",i]']
        else:
            raise ValueError("keyword is too short for the OSM data source.")
        blocks = "".join(
            f"node(around:{radius},{latitude:.6f},{longitude:.6f}){filt};"
            f"way(around:{radius},{latitude:.6f},{longitude:.6f}){filt};"
            for filt in filter_expressions
        )
        query = f"[out:json][timeout:20];({blocks});out center 60;"
        response: httpx.Response | None = None
        last_error: Exception | None = None
        for attempt in range(2):
            if attempt:
                await asyncio.sleep(2.0)
            try:
                async with httpx.AsyncClient(
                    timeout=35.0,
                    headers={"User-Agent": "GCAI-JARVIS/1.0"},
                ) as client:
                    candidate = await client.post(
                        "https://overpass-api.de/api/interpreter",
                        content=query.encode("utf-8"),
                    )
            except Exception as exc:
                last_error = exc
                continue
            if candidate.status_code == 200:
                response = candidate
                break
            last_error = RuntimeError(f"Overpass HTTP {candidate.status_code}")
        if response is None:
            raise RuntimeError(
                "OSM 附近数据服务暂时不可用"
                f"（{type(last_error).__name__}: {last_error}）；"
                "请稍后重试，或配置 JARVIS_AMAP_API_KEY 使用高德数据源。"
            )
        elements = response.json().get("elements") or []
        places: list[dict[str, Any]] = []
        seen: set[str] = set()
        for element in elements:
            tags = element.get("tags") or {}
            name = _clean_text(tags.get("name") or tags.get("name:zh"))
            center = element.get("center") or {}
            item_lat = element.get("lat") or center.get("lat")
            item_lon = element.get("lon") or center.get("lon")
            if not name or item_lat is None or item_lon is None:
                continue
            dedupe_key = f"{name}:{round(float(item_lat), 4)}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            address = " ".join(
                part
                for part in (
                    _clean_text(tags.get("addr:city")),
                    _clean_text(tags.get("addr:district")),
                    _clean_text(tags.get("addr:street"))
                    + _clean_text(tags.get("addr:housenumber")),
                )
                if part
            )
            places.append(
                {
                    "name": name,
                    "address": address or "—",
                    "distance_m": round(
                        _haversine_m(
                            latitude,
                            longitude,
                            float(item_lat),
                            float(item_lon),
                        )
                    ),
                    "type": _clean_text(
                        tags.get("amenity")
                        or tags.get("shop")
                        or tags.get("tourism")
                        or tags.get("office")
                        or ""
                    ),
                    "latitude": float(item_lat),
                    "longitude": float(item_lon),
                }
            )
        places.sort(key=lambda item: item.get("distance_m") or 10**9)
        return places[:limit]

    async def web_search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        max_results = max(1, min(max_results, 8))

        def search() -> list[dict[str, str]]:
            results = DDGS().text(query, max_results=max_results)
            return [
                {
                    "title": str(item.get("title", "")),
                    "url": str(item.get("href", "")),
                    "snippet": str(item.get("body", "")),
                }
                for item in results
            ]

        return await asyncio.to_thread(search)

    async def web_read_page(
        self,
        url: str,
        max_chars: int = 8_000,
    ) -> dict[str, Any]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Only absolute HTTP and HTTPS URLs are supported.")
        max_chars = max(500, min(int(max_chars), 50_000))
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "GCAI-JARVIS/0.1"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "html" in content_type:
            title, text, links = _extract_readable_html(response.text)
        elif "text" in content_type or "json" in content_type:
            title = ""
            text = response.text
            links = []
        else:
            raise ValueError(f"Unsupported content type: {content_type or 'unknown'}")
        return {
            "url": str(response.url),
            "status": response.status_code,
            "title": title,
            "text": text[:max_chars],
            "links": links[:20],
            "truncated": len(text) > max_chars,
        }

    async def browser_open_url(self, url: str) -> dict[str, Any]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Only absolute HTTP and HTTPS URLs are supported.")
        opened = await asyncio.to_thread(webbrowser.open, url, 2, True)
        return {"url": url, "opened": bool(opened)}

    async def get_system_info(self) -> dict[str, Any]:
        disk = shutil.disk_usage(self.settings.workspace)
        result: dict[str, Any] = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "workspace": str(self.settings.workspace),
            "disk_total_gb": round(disk.total / 1024**3, 1),
            "disk_free_gb": round(disk.free / 1024**3, 1),
        }
        result.update(self._memory_info())
        gpu = await self._gpu_name()
        if gpu:
            result["gpu"] = gpu
        return result

    @staticmethod
    def _memory_info() -> dict[str, float]:
        if os.name != "nt":
            return {}

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {}
        return {
            "memory_total_gb": round(status.ullTotalPhys / 1024**3, 1),
            "memory_available_gb": round(status.ullAvailPhys / 1024**3, 1),
            "memory_percent": float(status.dwMemoryLoad),
        }

    @staticmethod
    async def _gpu_name() -> str:
        if os.name != "nt":
            return ""
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    (
                        "$adapters = Get-CimInstance Win32_VideoController | "
                        "Where-Object { $_.Name -notmatch 'Virtual|Oray|AskLink|Basic' }; "
                        "if (-not $adapters) { "
                        "$adapters = Get-CimInstance Win32_VideoController }; "
                        "$adapters | Select-Object -First 1 -ExpandProperty Name"
                    ),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return completed.stdout.strip()

    async def list_workspace(self, path: str = ".") -> list[dict[str, str]]:
        target = self._resolve_workspace_path(path)
        if not target.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")
        if not target.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")

        entries = []
        for entry in sorted(
            target.iterdir(),
            key=lambda item: (not item.is_dir(), item.name.lower()),
        ):
            entries.append(
                {
                    "name": entry.name,
                    "type": "directory" if entry.is_dir() else "file",
                    "size": "" if entry.is_dir() else str(entry.stat().st_size),
                }
            )
            if len(entries) >= 200:
                break
        return entries

    async def read_workspace_file(self, path: str) -> dict[str, str]:
        target = self._resolve_workspace_path(path)
        if not target.exists():
            raise FileNotFoundError(f"File does not exist: {path}")
        if not target.is_file():
            raise IsADirectoryError(f"Not a file: {path}")
        size = target.stat().st_size
        if size > self.settings.max_file_bytes:
            raise ValueError(
                f"File is {size} bytes; limit is {self.settings.max_file_bytes} bytes."
            )
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Only UTF-8 text files can be read.") from exc
        return {"path": str(target.relative_to(self.settings.workspace)), "content": content}

    def _resolve_workspace_path(self, path: str) -> Path:
        workspace = self.settings.workspace.resolve()
        target = (workspace / path).resolve()
        if target != workspace and workspace not in target.parents:
            raise PermissionError("Path escapes the configured workspace.")
        return target

    async def computer_window_list(self) -> list[dict[str, Any]]:
        return await self.desktop.execute("window_list", enforce_permission=False)

    async def computer_screenshot(self) -> dict[str, Any]:
        image = await self.desktop.execute("screenshot", enforce_permission=False)
        artifacts = self.settings.data_dir / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        target = artifacts / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"
        target.write_bytes(image)
        return {"path": str(target), "bytes": len(image), "format": "png"}

    async def computer_analyze_screen(
        self,
        prompt: str = "Describe the screen and identify actionable UI elements.",
    ) -> dict[str, Any]:
        if self.vision_provider is None:
            raise RuntimeError("Vision model is not configured.")
        image = await self.desktop.execute("screenshot", enforce_permission=False)
        analyzer = VisionAnalyzer(self.vision_provider)
        description = await analyzer.describe(image, prompt)
        return {
            "description": description,
            "bytes": len(image),
            "format": "png",
        }

    async def computer_ui_tree(self, max_items: int = 120) -> list[dict[str, Any]]:
        return await self.desktop.execute(
            "ui_tree",
            {"max_items": max_items},
            enforce_permission=False,
        )

    async def computer_ui_invoke(
        self,
        name: str | None = None,
        automation_id: str | None = None,
    ) -> dict[str, Any]:
        await self.desktop.execute(
            "ui_invoke",
            {"name": name, "automation_id": automation_id},
            enforce_permission=False,
        )
        return {"invoked": True, "name": name, "automation_id": automation_id}

    async def computer_find_and_click(
        self,
        name: str | None = None,
        automation_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.desktop.execute(
            "find_and_click",
            {"name": name, "automation_id": automation_id},
            enforce_permission=False,
        )

    async def computer_open_app(self, application: str) -> dict[str, Any]:
        await self.desktop.execute(
            "app_open",
            {"application": application},
            enforce_permission=False,
        )
        return {"application": application, "opened": True}

    async def computer_close_app(self, application: str) -> dict[str, Any]:
        await self.desktop.execute(
            "app_close",
            {"application": application},
            enforce_permission=False,
        )
        return {"application": application, "closed": True}

    async def computer_focus_window(self, target: str) -> dict[str, Any]:
        await self.desktop.execute(
            "window_focus",
            {"target": target},
            enforce_permission=False,
        )
        return {"target": target, "focused": True}

    async def computer_keyboard_type(self, text: str) -> dict[str, Any]:
        await self.desktop.execute(
            "keyboard_type",
            {"text": text},
            enforce_permission=False,
        )
        return {"typed": True}

    async def computer_keyboard_press(self, key: str) -> dict[str, Any]:
        await self.desktop.execute(
            "keyboard_press",
            {"key": key},
            enforce_permission=False,
        )
        return {"key": key, "pressed": True}

    async def computer_mouse_click(
        self,
        x: int | None = None,
        y: int | None = None,
    ) -> dict[str, Any]:
        await self.desktop.execute(
            "mouse_click",
            {"x": x, "y": y},
            enforce_permission=False,
        )
        return {"clicked": True, "x": x, "y": y}

    async def computer_mouse_double_click(
        self,
        x: int | None = None,
        y: int | None = None,
    ) -> dict[str, Any]:
        await self.desktop.execute(
            "mouse_double_click",
            {"x": x, "y": y},
            enforce_permission=False,
        )
        return {"double_clicked": True, "x": x, "y": y}

    async def computer_scroll(self, delta: int) -> dict[str, Any]:
        await self.desktop.execute(
            "scroll",
            {"delta": delta},
            enforce_permission=False,
        )
        return {"scrolled": True, "delta": delta}

    async def computer_keyboard_hotkey(self, keys: list[str]) -> dict[str, Any]:
        await self.desktop.execute(
            "keyboard_hotkey",
            {"keys": keys},
            enforce_permission=False,
        )
        return {"keys": keys, "pressed": True}

    async def computer_clipboard_read(self) -> dict[str, Any]:
        text = await self.desktop.execute(
            "clipboard_read",
            {},
            enforce_permission=False,
        )
        return {"text": text}

    async def computer_clipboard_write(self, text: str) -> dict[str, Any]:
        await self.desktop.execute(
            "clipboard_write",
            {"text": text},
            enforce_permission=False,
        )
        return {"written": True, "characters": len(text)}

    async def run_workspace_command(
        self,
        command: str,
        args: list[str] | None = None,
        timeout: float = 60,
        cwd: str = ".",
    ) -> dict[str, Any]:
        args = args or []
        self._validate_workspace_command(command, args)
        working_directory = self._resolve_workspace_path(cwd)
        if not working_directory.exists() or not working_directory.is_dir():
            raise FileNotFoundError(f"Workspace directory does not exist: {cwd}")
        executable = self._resolve_command(command)
        process_args = [executable, *args]
        if executable.lower().endswith((".cmd", ".bat")):
            process_args = ["cmd.exe", "/d", "/s", "/c", executable, *args]
        process = await asyncio.create_subprocess_exec(
            *process_args,
            cwd=working_directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=max(1.0, min(float(timeout), 120.0)),
            )
        except TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await process.communicate()
        return {
            "command": command,
            "args": args,
            "cwd": str(working_directory.relative_to(self.settings.workspace)),
            "returncode": process.returncode,
            "timed_out": timed_out,
            "stdout": stdout.decode("utf-8", errors="replace")[-20_000:],
            "stderr": stderr.decode("utf-8", errors="replace")[-20_000:],
        }

    async def run_background_command(
        self,
        command: str,
        args: list[str] | None = None,
        timeout: float = 60,
        cwd: str = ".",
        name: str | None = None,
    ) -> dict[str, Any]:
        if self.scheduler is None:
            raise RuntimeError("Scheduler is not available.")
        args = args or []
        self._validate_workspace_command(command, args)
        job_name = name or f"background:{command}"

        async def run_job() -> dict[str, Any]:
            result = await self.run_workspace_command(
                command,
                args,
                timeout,
                cwd,
            )
            if result["timed_out"] or result["returncode"] != 0:
                raise RuntimeError(
                    f"Background command failed: {command} "
                    f"returncode={result['returncode']} "
                    f"stderr={result['stderr'][-1000:]}"
                )
            return {
                "command": command,
                "args": args,
                "cwd": result["cwd"],
                "returncode": result["returncode"],
                "stdout": result["stdout"][-4000:],
            }

        job = self.scheduler.add_once(
            job_name,
            run_job,
            run_after_seconds=0,
            notify_on_complete=True,
        )
        return {
            "id": job.id,
            "name": job.name,
            "status": job.status,
        }

    async def schedule_reminder(
        self,
        message: str,
        after_seconds: float,
        name: str | None = None,
    ) -> dict[str, Any]:
        if self.reminder_service is None:
            raise RuntimeError("Reminder service is not available.")
        reminder = self.reminder_service.schedule(
            message,
            after_seconds,
            name,
        )
        return reminder.to_dict()

    @staticmethod
    def _validate_workspace_command(command: str, args: list[str]) -> None:
        allowed_commands = {"python", "python3", "pytest", "ruff", "node", "pnpm", "npm", "git"}
        if command not in allowed_commands:
            raise PermissionError(f"Command is not allowlisted: {command}")
        if len(args) > 20 or any(len(str(item)) > 500 for item in args):
            raise PermissionError("Command arguments exceed the allowed bounds.")
        if any(any(character in str(item) for character in "&|<>^%") for item in args):
            raise PermissionError("Shell operator characters are not allowed in arguments.")

        if command == "git" and (
            not args or args[0] not in {"status", "diff", "log", "show", "branch", "rev-parse"}
        ):
            raise PermissionError("Only read-only git subcommands are allowed.")
        if command in {"pnpm", "npm"} and (
            not args or args[0] not in {"test", "run", "exec"}
        ):
            raise PermissionError("Only test, run, and exec package commands are allowed.")
        if command in {"python", "python3"} and (
            len(args) < 2
            or args[0] != "-m"
            or args[1] not in {"pytest", "ruff", "compileall"}
        ):
            raise PermissionError("Only pytest, ruff, and compileall Python modules are allowed.")
        if command == "node" and (
            not args
            or not (
                args[0] == "--version"
                or args[0].endswith((".js", ".mjs", ".cjs"))
            )
        ):
            raise PermissionError("Only Node version checks and workspace scripts are allowed.")

    def _resolve_command(self, command: str) -> str:
        if command in {"python", "python3"}:
            return sys.executable
        if command == "pytest":
            candidate = self.settings.workspace / ".venv" / "Scripts" / "pytest.exe"
            if candidate.exists():
                return str(candidate)
        if command == "ruff":
            candidate = self.settings.workspace / ".venv" / "Scripts" / "ruff.exe"
            if candidate.exists():
                return str(candidate)

        resolved = shutil.which(command)
        if resolved:
            return resolved

        node_root = Path.home() / ".local" / "node-v22.23.2-win-x64"
        candidates = {
            "node": [node_root / "node.exe"],
            "pnpm": [node_root / "pnpm.cmd", node_root / "pnpm.exe"],
            "npm": [node_root / "npm.cmd", node_root / "npm.exe"],
        }.get(command, [])
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        raise FileNotFoundError(f"Command is not installed: {command}")


class _ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self._skip_depth = 0
        self._in_title = False
        self._active_link: str | None = None
        self._active_link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style", "noscript", "template"}:
            self._skip_depth += 1
            return
        if normalized == "title":
            self._in_title = True
        if normalized == "a":
            href = dict(attrs).get("href")
            if href:
                self._active_link = href
                self._active_link_text = []

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style", "noscript", "template"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if normalized == "title":
            self._in_title = False
        if normalized == "a" and self._active_link:
            text = " ".join("".join(self._active_link_text).split())
            if text:
                self.links.append({"text": text, "url": self._active_link})
            self._active_link = None
            self._active_link_text = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        value = html.unescape(data).strip()
        if not value:
            return
        if self._in_title:
            self.title_parts.append(value)
        if self._active_link is not None:
            self._active_link_text.append(value)
        self.text_parts.append(value)


def _extract_readable_html(value: str) -> tuple[str, str, list[dict[str, str]]]:
    parser = _ReadableHTMLParser()
    parser.feed(value)
    parser.close()
    title = " ".join(" ".join(parser.title_parts).split())
    text = " ".join(" ".join(parser.text_parts).split())
    return title, html.unescape(text), parser.links


_OSM_CATEGORY_FILTERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("咖啡", "cafe", "coffee"), '["amenity"="cafe"]'),
    (
        ("火锅", "餐厅", "饭店", "餐馆", "美食", "快餐", "restaurant"),
        '["amenity"~"^(restaurant|fast_food)$"]',
    ),
    (("便利店",), '["shop"="convenience"]'),
    (("超市", "supermarket"), '["shop"="supermarket"]'),
    (("药店", "药房", "pharmacy"), '["amenity"="pharmacy"]'),
    (("银行", "bank"), '["amenity"="bank"]'),
    (("取款", "atm"), '["amenity"="atm"]'),
    (("加油", "fuel"), '["amenity"="fuel"]'),
    (("停车", "parking"), '["amenity"="parking"]'),
    (("厕所", "卫生间", "洗手间", "toilet"), '["amenity"="toilets"]'),
    (("医院", "hospital"), '["amenity"="hospital"]'),
    (("诊所", "clinic"), '["amenity"="clinic"]'),
    (("地铁", "metro", "subway"), '["railway"="station"]'),
    (("公交", "bus"), '["highway"="bus_stop"]'),
    (("酒店", "宾馆", "hotel"), '["tourism"="hotel"]'),
    (("充电", "charging"), '["amenity"="charging_station"]'),
    (("邮局", "邮政", "快递", "post"), '["amenity"="post_office"]'),
    (("网吧", "网咖", "internet"), '["amenity"="internet_cafe"]'),
)


def _nearby_osm_filter(keyword: str) -> str | None:
    lowered = keyword.lower()
    for triggers, filter_expression in _OSM_CATEGORY_FILTERS:
        if any(trigger in lowered for trigger in triggers):
            return filter_expression
    return None


def _geocode_candidates(query: str, home_city: str) -> list[str]:
    """Build geocode fallbacks: city-prefixed, original, then short prefixes."""
    base = query.strip()
    candidates: list[str] = []
    city = home_city.strip()
    if city and city not in base:
        candidates.append(f"{city}{base}")
    if base:
        candidates.append(base)
    for source in list(candidates):
        for cut in (3, 2):
            if len(source) > cut:
                candidates.append(source[:cut])
    seen: set[str] = set()
    ordered: list[str] = []
    for item in candidates:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _haversine_m(
    latitude1: float,
    longitude1: float,
    latitude2: float,
    longitude2: float,
) -> float:
    earth_radius = 6_371_000.0
    phi1 = math.radians(latitude1)
    phi2 = math.radians(latitude2)
    delta_phi = math.radians(latitude2 - latitude1)
    delta_lambda = math.radians(longitude2 - longitude1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * earth_radius * math.asin(math.sqrt(a))


def _clean_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "".join(str(item) for item in value).strip()
    return str(value or "").strip()


def _to_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _tool_category(capability: str, name: str) -> str:
    if capability == "mcp":
        return "MCP"
    if capability == "browser":
        return "BROWSER"
    if capability == "computer":
        return "DESKTOP"
    if capability == "vision":
        return "VISION"
    if name.startswith(("remember", "recall", "update_memory", "forget")):
        return "MEMORY"
    if name in {"list_workspace", "read_workspace_file", "run_workspace_command"}:
        return "FILE"
    if name in {"schedule_reminder", "run_background_command"}:
        return "SCHEDULER"
    if name in {"get_weather", "web_search", "web_read_page", "browser_open_url"}:
        return "NETWORK"
    return "SYSTEM"


def _safe_tool_name(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in {"_", "-"} else "_"
        for character in value
    ).strip("_") or "tool"


def _json_safe_tool_output(tool_name: str, value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): _json_safe_tool_output(tool_name, item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_tool_output(tool_name, item) for item in value]
    raise TypeError(
        f"Tool {tool_name} returned unsupported output type "
        f"{type(value).__name__}."
    )


def _mcp_handler(manager: MCPManager, server: str, tool_name: str) -> ToolHandler:
    async def call_mcp(**arguments: Any) -> Any:
        return await manager.call_tool(server, tool_name, arguments)

    return call_mcp
