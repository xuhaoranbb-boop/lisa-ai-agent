from __future__ import annotations

from enum import StrEnum
from typing import Any

from jarvis.runtime.permissions import PermissionManager, PermissionPolicy


class PermissionMode(StrEnum):
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"
    ASK_EVERY_TIME = "ASK_EVERY_TIME"


PERMISSION_CATEGORIES = (
    "desktop",
    "files",
    "browser",
    "network",
    "mcp",
    "system",
)


class PermissionCenter:
    """User-configurable category policy enforced by PermissionManager."""

    def __init__(
        self,
        modes: dict[str, str] | None = None,
        *,
        on_change: Any | None = None,
    ) -> None:
        self.on_change = on_change
        self._modes = {
            category: PermissionMode.ASK_EVERY_TIME
            for category in PERMISSION_CATEGORIES
        }
        for category, mode in (modes or {}).items():
            normalized = _normalize_category(category)
            try:
                self._modes[normalized] = PermissionMode(str(mode).upper())
            except ValueError:
                continue

    def modes(self) -> dict[str, str]:
        return {key: str(value) for key, value in self._modes.items()}

    def set_mode(self, category: str, mode: PermissionMode | str) -> dict[str, str]:
        normalized = _normalize_category(category)
        if normalized not in PERMISSION_CATEGORIES:
            raise ValueError(f"Unknown permission category: {category}")
        self._modes[normalized] = PermissionMode(str(mode).upper())
        if callable(self.on_change):
            self.on_change(self.modes())
        return self.modes()

    def apply(self, permission_manager: PermissionManager) -> None:
        permission_manager.policy.category_modes = {
            key: str(value)
            for key, value in self._modes.items()
        }

    def to_policy(self, base: PermissionPolicy | None = None) -> PermissionPolicy:
        policy = base or PermissionPolicy()
        policy.category_modes = {
            key: str(value)
            for key, value in self._modes.items()
        }
        return policy

    def to_dict(self) -> dict[str, Any]:
        return {
            "categories": [
                {
                    "name": category.upper(),
                    "key": category,
                    "mode": str(self._modes[category]),
                    "options": [str(mode) for mode in PermissionMode],
                }
                for category in PERMISSION_CATEGORIES
            ],
            "high_risk_default": str(PermissionMode.ASK_EVERY_TIME),
        }


def permission_category(
    action: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> str:
    metadata = metadata or {}
    explicit = str(metadata.get("category") or metadata.get("capability") or "")
    if explicit:
        normalized = _normalize_category(explicit)
        if normalized in PERMISSION_CATEGORIES:
            return normalized
    lowered = action.lower()
    if lowered.startswith("mcp_"):
        return "mcp"
    if any(token in lowered for token in ("browser", "web_", "url", "download")):
        return "browser"
    if any(
        token in lowered
        for token in ("file", "path", "folder", "workspace", "read_", "write_")
    ):
        return "files"
    if any(
        token in lowered
        for token in (
            "mouse",
            "keyboard",
            "window",
            "app_",
            "screenshot",
            "clipboard",
            "desktop",
            "ui_",
        )
    ):
        return "desktop"
    if any(token in lowered for token in ("network", "weather", "search")):
        return "network"
    return "system"


def _normalize_category(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "desktop_control": "desktop",
        "computer": "desktop",
        "file": "files",
        "plugins": "system",
    }
    return aliases.get(normalized, normalized)
