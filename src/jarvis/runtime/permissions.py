from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from jarvis.runtime.contracts import PermissionLevel
from jarvis.runtime.events import EventBus, EventType, RuntimeEvent


class PermissionDenied(Exception):
    def __init__(self, action: str, reason: str) -> None:
        super().__init__(reason)
        self.action = action
        self.reason = reason


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    level: PermissionLevel
    action: str
    resource: str | None = None
    reason: str = ""
    requires_confirmation: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


ConfirmationHandler = Callable[
    [PermissionLevel, str, str | None, dict[str, Any]],
    bool | str | Awaitable[bool | str],
]


class ConfirmationManager:
    """Adapt approval requests into one-step or two-step confirmation flows."""

    def __init__(self, handler: ConfirmationHandler) -> None:
        self.handler = handler

    async def request(
        self,
        level: PermissionLevel,
        action: str,
        resource: str | None,
        metadata: dict[str, Any],
    ) -> bool | str:
        if level in {
            PermissionLevel.READ_ONLY,
            PermissionLevel.LOW_RISK,
        }:
            return True
        if level == PermissionLevel.CRITICAL:
            first = await self._call(
                level,
                action,
                resource,
                {
                    **metadata,
                    "confirmation_stage": 1,
                    "confirmation_message": (
                        f"Critical action: {action}. Review the target carefully."
                    ),
                },
            )
            if not _confirmation_allowed(first):
                return first
            return await self._call(
                level,
                action,
                resource,
                {
                    **metadata,
                    "confirmation_stage": 2,
                    "confirmation_message": (
                        "Final confirmation required. This action may be irreversible."
                    ),
                },
            )
        return await self._call(level, action, resource, metadata)

    async def _call(
        self,
        level: PermissionLevel,
        action: str,
        resource: str | None,
        metadata: dict[str, Any],
    ) -> bool | str:
        result = self.handler(level, action, resource, metadata)
        if inspect.isawaitable(result):
            result = await result
        return result


def _confirmation_allowed(value: bool | str) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {
            ApprovalDecision.ALLOW_ONCE,
            ApprovalDecision.ALLOW_TASK,
        }
    return bool(value)


class ApprovalDecision(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_TASK = "allow_task"
    DENY = "deny"


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    level: PermissionLevel
    action: str
    resource: str | None
    metadata: dict[str, Any]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "level": str(self.level),
            "action": self.action,
            "resource": self.resource,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


@dataclass
class PermissionPolicy:
    auto_approve: set[PermissionLevel] = field(
        default_factory=lambda: {
            PermissionLevel.READ_ONLY,
            PermissionLevel.LOW_RISK,
        }
    )
    allowed_apps: tuple[str, ...] = ()
    allowed_folders: tuple[str, ...] = ()
    allowed_commands: tuple[str, ...] = ()
    blocked_apps: tuple[str, ...] = ()
    blocked_paths: tuple[str, ...] = ()
    blocked_commands: tuple[str, ...] = ()
    allowed_message_recipients: tuple[str, ...] = ()
    category_modes: dict[str, str] = field(default_factory=dict)


class PermissionManager:
    """Central policy gate for every executable runtime action."""

    def __init__(
        self,
        policy: PermissionPolicy | None = None,
        confirmation_handler: ConfirmationHandler | None = None,
        confirmation_manager: ConfirmationManager | None = None,
    ) -> None:
        self.policy = policy or PermissionPolicy()
        self.confirmation_handler = confirmation_handler
        self.confirmation_manager = confirmation_manager
        self._one_shot: set[tuple[str, str | None]] = set()
        self._task_grants: set[tuple[str, str | None]] = set()

    def grant_once(self, action: str, resource: str | None = None) -> None:
        self._one_shot.add((action, resource))

    def grant_task(self, action: str, resource: str | None = None) -> None:
        self._task_grants.add((action, resource))

    async def decide(
        self,
        level: PermissionLevel | str,
        action: str,
        resource: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PermissionDecision:
        resolved_level = PermissionLevel(level)
        metadata = metadata or {}
        resource = resource.strip() if isinstance(resource, str) else resource

        if resolved_level == PermissionLevel.BLOCKED:
            return PermissionDecision(
                allowed=False,
                level=resolved_level,
                action=action,
                resource=resource,
                reason="This action is blocked by policy.",
                metadata=metadata,
            )

        category = _permission_category(action, metadata)
        category_mode = str(
            self.policy.category_modes.get(category, "")
        ).strip().upper()
        if category_mode == "DENIED":
            return PermissionDecision(
                allowed=False,
                level=resolved_level,
                action=action,
                resource=resource,
                reason=f"The {category} permission category is denied.",
                metadata=metadata,
            )

        blocked_reason = self._blocked_reason(action, resource)
        if blocked_reason:
            return PermissionDecision(
                allowed=False,
                level=resolved_level,
                action=action,
                resource=resource,
                reason=blocked_reason,
                metadata=metadata,
            )

        allow_reason = self._allowlist_reason(action, resource, metadata)
        if allow_reason:
            return PermissionDecision(
                allowed=False,
                level=resolved_level,
                action=action,
                resource=resource,
                reason=allow_reason,
                metadata=metadata,
            )

        if self._is_trusted_message_recipient(action, resource, metadata):
            return PermissionDecision(
                allowed=True,
                level=resolved_level,
                action=action,
                resource=resource,
                reason="Recipient is in the trusted message allowlist.",
                metadata=metadata,
            )

        once_key = (action, resource)
        if once_key in self._one_shot:
            self._one_shot.discard(once_key)
            return PermissionDecision(
                allowed=True,
                level=resolved_level,
                action=action,
                resource=resource,
                reason="Allowed once by the user.",
                metadata=metadata,
            )
        if once_key in self._task_grants:
            return PermissionDecision(
                allowed=True,
                level=resolved_level,
                action=action,
                resource=resource,
                reason="Allowed for the current task by the user.",
                metadata=metadata,
            )

        force_ask = category_mode == "ASK_EVERY_TIME"
        if resolved_level in self.policy.auto_approve and not force_ask:
            return PermissionDecision(
                allowed=True,
                level=resolved_level,
                action=action,
                resource=resource,
                reason="Allowed by default policy.",
                metadata=metadata,
            )

        if (
            category_mode == "ALLOWED"
            and resolved_level
            in {
                PermissionLevel.READ_ONLY,
                PermissionLevel.LOW_RISK,
                PermissionLevel.NORMAL,
                PermissionLevel.MEDIUM_RISK,
            }
            and not force_ask
        ):
            return PermissionDecision(
                allowed=True,
                level=resolved_level,
                action=action,
                resource=resource,
                reason=f"The {category} permission category is allowed.",
                metadata=metadata,
            )

        if force_ask or resolved_level in {
            PermissionLevel.NORMAL,
            PermissionLevel.MEDIUM_RISK,
            PermissionLevel.HIGH_RISK,
            PermissionLevel.CRITICAL,
        }:
            confirmation_handler = (
                self.confirmation_manager.request
                if self.confirmation_manager is not None
                else self.confirmation_handler
            )
            if confirmation_handler is None:
                return PermissionDecision(
                    allowed=False,
                    level=resolved_level,
                    action=action,
                    resource=resource,
                    reason="High-risk action requires explicit confirmation.",
                    requires_confirmation=True,
                    metadata=metadata,
                )
            confirmed = confirmation_handler(resolved_level, action, resource, metadata)
            if inspect.isawaitable(confirmed):
                confirmed = await confirmed
            decision = str(confirmed).strip().lower() if isinstance(confirmed, str) else ""
            if decision == ApprovalDecision.ALLOW_TASK:
                self.grant_task(action, resource)
            elif decision == ApprovalDecision.ALLOW_ONCE:
                self.grant_once(action, resource)
            allowed = (
                decision in {
                    ApprovalDecision.ALLOW_ONCE,
                    ApprovalDecision.ALLOW_TASK,
                }
                if isinstance(confirmed, str)
                else bool(confirmed)
            )
            return PermissionDecision(
                allowed=allowed,
                level=resolved_level,
                action=action,
                resource=resource,
                reason=(
                    "User approved the action."
                    if allowed
                    else "User rejected the action."
                ),
                metadata=metadata,
            )

        return PermissionDecision(
            allowed=False,
            level=resolved_level,
            action=action,
            resource=resource,
            reason="Action level is not auto-approved.",
            requires_confirmation=True,
            metadata=metadata,
        )

    async def require(
        self,
        level: PermissionLevel | str,
        action: str,
        resource: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PermissionDecision:
        decision = await self.decide(level, action, resource, metadata)
        if not decision.allowed:
            raise PermissionDenied(action, decision.reason)
        return decision

    def _blocked_reason(self, action: str, resource: str | None) -> str | None:
        if self._matches(resource, self.policy.blocked_paths):
            return "The requested path is blocked by policy."
        if self._matches(resource, self.policy.blocked_apps):
            return "The requested application is blocked by policy."
        if self._matches(action, self.policy.blocked_commands):
            return "The requested command is blocked by policy."
        return None

    def _allowlist_reason(
        self,
        action: str,
        resource: str | None,
        metadata: dict[str, Any],
    ) -> str | None:
        resource_kind = str(metadata.get("resource_type") or self._infer_resource_kind(action))
        if (
            resource_kind == "path"
            and self.policy.allowed_folders
            and not self._matches(resource, self.policy.allowed_folders)
        ):
            return "The requested path is outside the allowed folders."
        if (
            resource_kind == "app"
            and self.policy.allowed_apps
            and not self._matches(resource, self.policy.allowed_apps)
        ):
            return "The requested application is not in the allowed list."
        if (
            resource_kind == "command"
            and self.policy.allowed_commands
            and not self._matches(action, self.policy.allowed_commands)
        ):
            return "The requested command is not in the allowed list."
        return None

    def _is_trusted_message_recipient(
        self,
        action: str,
        resource: str | None,
        metadata: dict[str, Any],
    ) -> bool:
        if not self.policy.allowed_message_recipients or not resource:
            return False
        lowered = f"{action} {metadata.get('category') or ''}".lower()
        if "message" not in lowered and "send" not in lowered:
            return False
        recipient = resource.strip().lower()
        if recipient == "*":
            return False
        return any(
            recipient == allowed.strip().lower()
            for allowed in self.policy.allowed_message_recipients
            if allowed.strip() and allowed.strip() != "*"
        )

    @staticmethod
    def _infer_resource_kind(action: str) -> str:
        lowered = action.lower()
        if "app" in lowered or "application" in lowered:
            return "app"
        if any(token in lowered for token in ("file", "path", "folder", "directory", "workspace")):
            return "path"
        return "command"

    @staticmethod
    def _matches(value: str | None, patterns: tuple[str, ...]) -> bool:
        if not patterns or value is None:
            return False
        normalized = str(Path(value).expanduser()) if value else value
        return any(fnmatch(value, pattern) or fnmatch(normalized, pattern) for pattern in patterns)


def _permission_category(action: str, metadata: dict[str, Any]) -> str:
    explicit = str(metadata.get("category") or metadata.get("capability") or "")
    normalized = explicit.strip().lower().replace("-", "_")
    if normalized:
        return {
            "desktop_control": "desktop",
            "computer": "desktop",
            "file": "files",
        }.get(normalized, normalized)
    lowered = action.lower()
    if lowered.startswith("mcp_"):
        return "mcp"
    if any(token in lowered for token in ("browser", "web_", "url")):
        return "browser"
    if any(token in lowered for token in ("file", "path", "folder", "workspace")):
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
    return "system"


class ApprovalBroker:
    """Wait for a desktop confirmation and publish the pending request."""

    def __init__(
        self,
        event_bus: EventBus | None = None,
        *,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.event_bus = event_bus or EventBus()
        self.timeout_seconds = timeout_seconds
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Future[ApprovalDecision]]] = {}
        self._lock = asyncio.Lock()

    async def request(
        self,
        level: PermissionLevel,
        action: str,
        resource: str | None,
        metadata: dict[str, Any],
    ) -> ApprovalDecision:
        loop = asyncio.get_running_loop()
        request = ApprovalRequest(
            id=uuid.uuid4().hex,
            level=level,
            action=action,
            resource=resource,
            metadata=metadata,
            created_at=time.time(),
        )
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        async with self._lock:
            self._pending[request.id] = (request, future)

        await self.event_bus.emit_async(
            RuntimeEvent(
                type=EventType.PERMISSION_REQUEST,
                data=request.to_dict(),
            )
        )
        try:
            decision = await asyncio.wait_for(future, timeout=self.timeout_seconds)
        except TimeoutError:
            decision = ApprovalDecision.DENY
        finally:
            async with self._lock:
                self._pending.pop(request.id, None)

        await self.event_bus.emit_async(
            RuntimeEvent(
                type=(
                    EventType.PERMISSION_GRANTED
                    if decision in {ApprovalDecision.ALLOW_ONCE, ApprovalDecision.ALLOW_TASK}
                    else EventType.PERMISSION_DENIED
                ),
                data={**request.to_dict(), "decision": str(decision)},
            )
        )
        await self.event_bus.emit_async(
            RuntimeEvent(
                type=EventType.PERMISSION_RESOLVED,
                data={**request.to_dict(), "decision": str(decision)},
            )
        )
        return decision

    async def resolve(
        self,
        request_id: str,
        decision: ApprovalDecision | str,
    ) -> bool:
        resolved_decision = ApprovalDecision(decision)
        async with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            return False
        request, future = pending
        if future.done():
            return False
        future.set_result(resolved_decision)
        return True

    async def pending(self) -> list[ApprovalRequest]:
        async with self._lock:
            return [request for request, _future in self._pending.values()]
