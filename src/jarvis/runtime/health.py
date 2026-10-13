from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
    OFFLINE = "OFFLINE"


@dataclass(frozen=True)
class ComponentHealth:
    name: str
    status: HealthStatus
    latency_ms: float = 0.0
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": str(self.status),
            "latency_ms": self.latency_ms,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class HealthReport:
    status: HealthStatus
    components: list[ComponentHealth]
    checked_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "components": [component.to_dict() for component in self.components],
            "checked_at": self.checked_at,
        }


class HealthChecker:
    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge

    async def check(self) -> HealthReport:
        checks = await asyncio.gather(
            self._check_database(),
            self._check_memory(),
            self._check_tasks(),
            self._check_llm(),
            self._check_mcp(),
            self._check_vision(),
            self._check_browser(),
            self._check_desktop(),
            self._check_voice(),
            return_exceptions=True,
        )
        components: list[ComponentHealth] = []
        for item in checks:
            if isinstance(item, Exception):
                components.append(
                    ComponentHealth(
                        name="unknown",
                        status=HealthStatus.ERROR,
                        message=f"{type(item).__name__}: {item}",
                    )
                )
            else:
                components.append(item)
        status = _aggregate_status(components)
        return HealthReport(
            status=status,
            components=components,
            checked_at=time.time(),
        )

    async def _check_database(self) -> ComponentHealth:
        started = time.perf_counter()
        try:
            self.bridge.memory.list_memories(1)
            self.bridge.task_engine.list_tasks(1)
            self.bridge.execution_engine.store.list(1)
            return ComponentHealth(
                name="database",
                status=HealthStatus.HEALTHY,
                latency_ms=_elapsed_ms(started),
            )
        except Exception as exc:
            return ComponentHealth(
                name="database",
                status=HealthStatus.ERROR,
                latency_ms=_elapsed_ms(started),
                message=str(exc),
            )

    async def _check_memory(self) -> ComponentHealth:
        started = time.perf_counter()
        try:
            count = len(self.bridge.memory.list_memories(500))
            return ComponentHealth(
                name="memory",
                status=HealthStatus.HEALTHY,
                latency_ms=_elapsed_ms(started),
                details={"sampled_count": count},
            )
        except Exception as exc:
            return ComponentHealth(
                name="memory",
                status=HealthStatus.ERROR,
                latency_ms=_elapsed_ms(started),
                message=str(exc),
            )

    async def _check_tasks(self) -> ComponentHealth:
        started = time.perf_counter()
        try:
            tasks = self.bridge.task_engine.list_tasks(100)
            active = [
                task for task in tasks if task.status not in {"COMPLETED", "CANCELLED"}
            ]
            return ComponentHealth(
                name="tasks",
                status=HealthStatus.HEALTHY,
                latency_ms=_elapsed_ms(started),
                details={"total": len(tasks), "active": len(active)},
            )
        except Exception as exc:
            return ComponentHealth(
                name="tasks",
                status=HealthStatus.ERROR,
                latency_ms=_elapsed_ms(started),
                message=str(exc),
            )

    async def _check_llm(self) -> ComponentHealth:
        started = time.perf_counter()
        provider = self.bridge.router.provider_for("chat")
        warmup_error = str(getattr(self.bridge, "_llm_warmup_error", "") or "")
        return ComponentHealth(
            name="llm",
            status=(
                HealthStatus.HEALTHY
                if provider is not None and not warmup_error
                else HealthStatus.DEGRADED
                if provider is not None
                else HealthStatus.OFFLINE
            ),
            message=warmup_error,
            latency_ms=_elapsed_ms(started),
            details={"provider": getattr(provider, "name", "unknown")},
        )

    async def _check_mcp(self) -> ComponentHealth:
        started = time.perf_counter()
        configured = len(getattr(self.bridge.mcp_manager, "_configs", {}))
        connected = len(getattr(self.bridge.mcp_manager, "_clients", {}))
        if configured == 0:
            status = HealthStatus.HEALTHY
        elif connected == configured:
            status = HealthStatus.HEALTHY
        elif connected:
            status = HealthStatus.DEGRADED
        else:
            status = HealthStatus.OFFLINE
        return ComponentHealth(
            name="mcp",
            status=status,
            latency_ms=_elapsed_ms(started),
            details={"configured": configured, "connected": connected},
        )

    async def _check_vision(self) -> ComponentHealth:
        available = self.bridge.screen_observer.provider is not None
        return ComponentHealth(
            name="vision",
            status=HealthStatus.HEALTHY if available else HealthStatus.DEGRADED,
            message="" if available else "Vision model is not configured.",
        )

    async def _check_browser(self) -> ComponentHealth:
        available = self.bridge.browser_tool is not None
        return ComponentHealth(
            name="browser",
            status=HealthStatus.HEALTHY if available else HealthStatus.OFFLINE,
            details={"backend": self.bridge.settings.browser_backend},
        )

    async def _check_desktop(self) -> ComponentHealth:
        try:
            observation = await self.bridge.toolbox.desktop.observe(
                include_screenshot=False
            )
            return ComponentHealth(
                name="desktop",
                status=HealthStatus.HEALTHY,
                details={"windows": len(observation.windows)},
            )
        except Exception as exc:
            return ComponentHealth(
                name="desktop",
                status=HealthStatus.ERROR,
                message=str(exc),
            )

    async def _check_voice(self) -> ComponentHealth:
        asr = self.bridge.whisper is not None
        tts = self.bridge._speech_synthesizer is not None
        status = (
            HealthStatus.HEALTHY
            if asr and tts
            else HealthStatus.DEGRADED
        )
        return ComponentHealth(
            name="voice",
            status=status,
            details={"asr": asr, "tts": tts},
        )


def _aggregate_status(components: list[ComponentHealth]) -> HealthStatus:
    statuses = {component.status for component in components}
    if HealthStatus.OFFLINE in statuses:
        return HealthStatus.OFFLINE
    if HealthStatus.ERROR in statuses:
        return HealthStatus.ERROR
    if HealthStatus.DEGRADED in statuses:
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1_000, 3)
