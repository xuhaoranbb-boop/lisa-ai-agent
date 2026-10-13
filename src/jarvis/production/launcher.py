from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from jarvis.runtime.health import HealthReport, HealthStatus

StageCallable = Callable[[], Awaitable[None]]


class LauncherState(StrEnum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    CHECKING = "CHECKING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


@dataclass
class LauncherStatus:
    state: LauncherState = LauncherState.CREATED
    current_stage: str = ""
    completed_stages: list[str] = field(default_factory=list)
    failed_stage: str = ""
    error: str = ""
    started_at: float = 0.0
    ready_at: float = 0.0
    startup_time_ms: float = 0.0
    health: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "current_stage": self.current_stage,
            "completed_stages": self.completed_stages,
            "failed_stage": self.failed_stage,
            "error": self.error,
            "started_at": self.started_at,
            "ready_at": self.ready_at,
            "startup_time_ms": self.startup_time_ms,
            "health": self.health,
        }


class JarvisLauncher:
    """Run deterministic startup stages and expose readiness."""

    def __init__(self) -> None:
        self.status = LauncherStatus()
        self._stages: list[tuple[str, StageCallable]] = []

    def add_stage(self, name: str, callback: StageCallable) -> None:
        self._stages.append((name, callback))

    async def run(self, health_provider: Callable[[], Awaitable[HealthReport]]) -> LauncherStatus:
        self.status = LauncherStatus(
            state=LauncherState.STARTING,
            started_at=time.time(),
        )
        try:
            for name, callback in self._stages:
                self.status.current_stage = name
                await callback()
                self.status.completed_stages.append(name)
            self.status.state = LauncherState.CHECKING
            self.status.current_stage = "health_check"
            report = await health_provider()
            self.status.health = report.to_dict()
            if report.status == HealthStatus.HEALTHY:
                self.status.state = LauncherState.READY
            elif report.status in {HealthStatus.DEGRADED, HealthStatus.OFFLINE}:
                self.status.state = LauncherState.DEGRADED
            else:
                self.status.state = LauncherState.FAILED
            self.status.ready_at = time.time()
            self.status.startup_time_ms = round(
                (self.status.ready_at - self.status.started_at) * 1_000,
                3,
            )
        except Exception as exc:
            self.status.state = LauncherState.FAILED
            self.status.failed_stage = self.status.current_stage
            self.status.error = f"{type(exc).__name__}: {exc}"
        return self.status

    def mark_stopped(self) -> None:
        self.status.state = LauncherState.STOPPED
        self.status.current_stage = "stopped"
