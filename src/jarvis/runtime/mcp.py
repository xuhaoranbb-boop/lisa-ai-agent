from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from jarvis.runtime.contracts import PermissionLevel, ToolSpec
from jarvis.runtime.events import EventBus, EventType, RuntimeEvent
from jarvis.runtime.tooling import RuntimeTool, ToolRegistry


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: tuple[str, ...]
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class MCPTool:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def qualified_name(self) -> str:
        return f"{self.server}.{self.name}"


class MCPClient(Protocol):
    async def connect(self) -> None: ...
    async def list_tools(self) -> list[MCPTool]: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...
    async def close(self) -> None: ...


class StdioMCPClient:
    """Minimal JSON-RPC MCP client over newline-delimited stdio."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        env = os.environ.copy()
        env.update(self.config.env)
        self._process = await asyncio.create_subprocess_exec(
            *self.config.command,
            cwd=self.config.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "gcai-jarvis", "version": "0.1.0"},
            },
        )
        await self._notify("notifications/initialized", {})

    async def list_tools(self) -> list[MCPTool]:
        result = await self._request("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        return [
            MCPTool(
                server=self.config.name,
                name=str(item.get("name") or ""),
                description=str(item.get("description") or ""),
                input_schema=dict(item.get("inputSchema") or {}),
            )
            for item in tools
            if isinstance(item, dict) and item.get("name")
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._request(
            "tools/call",
            {"name": name, "arguments": arguments},
        )

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise RuntimeError(f"MCP server {self.config.name} is not connected.")

        async with self._lock:
            request_id = self._next_id
            self._next_id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            process.stdin.write(
                (json.dumps(payload, ensure_ascii=False) + "\n").encode()
            )
            await process.stdin.drain()

            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=self.config.timeout_seconds,
                )
            except TimeoutError:
                await self._notify("notifications/cancelled", {"requestId": request_id})
                raise

            if not line:
                raise RuntimeError(f"MCP server {self.config.name} closed stdout.")
            response = json.loads(line.decode())
            if response.get("id") != request_id:
                raise RuntimeError(
                    f"MCP server {self.config.name} returned response id "
                    f"{response.get('id')} for request {request_id}."
                )
            if "error" in response:
                raise RuntimeError(str(response["error"]))
            return response.get("result")

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        process.stdin.write(
            (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        )
        await process.stdin.drain()


class MCPManager:
    """Connect, discover, call, and reconnect MCP servers."""

    def __init__(
        self,
        event_bus: EventBus | None = None,
        client_factory: Callable[[MCPServerConfig], MCPClient] | None = None,
    ) -> None:
        self.event_bus = event_bus or EventBus()
        self.client_factory = client_factory or StdioMCPClient
        self._configs: dict[str, MCPServerConfig] = {}
        self._clients: dict[str, MCPClient] = {}
        self._tools: dict[str, MCPTool] = {}

    async def connect(self, config: MCPServerConfig) -> list[MCPTool]:
        existing = self._clients.pop(config.name, None)
        if existing is not None:
            await existing.close()
        self._tools = {
            name: tool
            for name, tool in self._tools.items()
            if tool.server != config.name
        }
        self._configs[config.name] = config
        agent, tools = await self._connect_client(config)
        self._clients[config.name] = agent
        for tool in tools:
            self._tools[tool.qualified_name] = tool
        await self._emit(
            EventType.MCP_CONNECTED,
            {"server": config.name, "tools": [tool.qualified_name for tool in tools]},
        )
        return tools

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        config = self._configs.get(server_name)
        if config is None:
            raise KeyError(f"Unknown MCP server: {server_name}")
        client = self._clients.get(server_name)
        if client is None:
            client, _tools = await self._connect_client(config)
            self._clients[server_name] = client

        await self._emit(
            EventType.MCP_TOOL_CALL,
            {"server": server_name, "tool": tool_name, "arguments": arguments},
        )
        try:
            result = await client.call_tool(tool_name, arguments)
        except Exception as first_error:
            try:
                await client.close()
                client, tools = await self._connect_client(config)
                self._clients[server_name] = client
                for tool in tools:
                    self._tools[tool.qualified_name] = tool
                await self._emit(
                    EventType.MCP_RECONNECTED,
                    {
                        "server": server_name,
                        "tool": tool_name,
                        "after_error": str(first_error),
                    },
                )
                result = await client.call_tool(tool_name, arguments)
            except Exception as exc:
                await self._emit(
                    EventType.MCP_ERROR,
                    {
                        "server": server_name,
                        "tool": tool_name,
                        "error": str(exc),
                    },
                )
                raise
        await self._emit(
            EventType.MCP_TOOL_RESULT,
            {"server": server_name, "tool": tool_name, "result": result},
        )
        return result

    def tools(self) -> list[MCPTool]:
        return list(self._tools.values())

    async def close(self) -> None:
        for client in list(self._clients.values()):
            await client.close()
        self._clients.clear()
        self._tools.clear()

    async def _emit(self, event_type: EventType, data: dict[str, Any]) -> None:
        await self.event_bus.emit_async(RuntimeEvent(type=event_type, data=data))

    async def _connect_client(
        self,
        config: MCPServerConfig,
    ) -> tuple[MCPClient, list[MCPTool]]:
        client = self.client_factory(config)
        await client.connect()
        tools = await client.list_tools()
        return client, tools


class MCPToolAdapter:
    """Register discovered MCP tools into the central ToolRegistry."""

    def __init__(
        self,
        manager: MCPManager,
        *,
        permission_level: PermissionLevel = PermissionLevel.HIGH_RISK,
    ) -> None:
        self.manager = manager
        self.permission_level = permission_level

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        names: list[str] = []
        for tool in self.manager.tools():

            async def handler(
                _server: str = tool.server,
                _tool: str = tool.name,
                **arguments: Any,
            ) -> Any:
                return await self.manager.call_tool(_server, _tool, arguments)

            registry.register(
                RuntimeTool(
                    spec=ToolSpec(
                        name=tool.qualified_name,
                        description=tool.description,
                        parameters=tool.input_schema,
                        permission=self.permission_level,
                        capability="mcp",
                        category="MCP",
                        risk_level=self.permission_level,
                        trusted_output=False,
                    ),
                    handler=handler,
                )
            )
            names.append(tool.qualified_name)
        return names
