from __future__ import annotations

import json

from jarvis.config import Settings
from jarvis.runtime.contracts import PermissionLevel
from jarvis.runtime.mcp import MCPServerConfig
from jarvis.runtime.permissions import PermissionPolicy


def csv_tuple(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def permission_policy_from_settings(settings: Settings) -> PermissionPolicy:
    levels = {
        item.strip().upper()
        for item in csv_tuple(settings.permission_auto_approve)
    }
    auto_approve = {
        PermissionLevel(level)
        for level in levels
        if level in PermissionLevel._value2member_map_
    }
    return PermissionPolicy(
        auto_approve=auto_approve,
        allowed_apps=csv_tuple(settings.permission_allowed_apps),
        allowed_folders=csv_tuple(settings.permission_allowed_folders),
        allowed_commands=csv_tuple(settings.permission_allowed_commands),
        allowed_message_recipients=csv_tuple(
            settings.permission_allowed_message_recipients
        ),
        blocked_apps=csv_tuple(settings.permission_blocked_apps),
        blocked_paths=csv_tuple(settings.permission_blocked_paths),
        blocked_commands=csv_tuple(settings.permission_blocked_commands),
    )


def mcp_server_configs_from_settings(settings: Settings) -> list[MCPServerConfig]:
    raw = settings.mcp_servers.strip()
    if not raw:
        return []
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("JARVIS_MCP_SERVERS must be a JSON array.")

    configs: list[MCPServerConfig] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError("Each MCP server entry must be an object.")
        name = str(item.get("name") or "").strip()
        command = item.get("command")
        if not name or not isinstance(command, list) or not command:
            raise ValueError("Each MCP server requires a name and a non-empty command.")
        configs.append(
            MCPServerConfig(
                name=name,
                command=tuple(str(part) for part in command),
                cwd=str(item["cwd"]) if item.get("cwd") else None,
                env={
                    str(key): str(value)
                    for key, value in dict(item.get("env") or {}).items()
                },
                timeout_seconds=float(item.get("timeout_seconds") or 30.0),
            )
        )
    return configs
