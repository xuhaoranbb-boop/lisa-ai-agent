from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from jarvis.runtime.health import ComponentHealth, HealthStatus

HealthCallable = Callable[[], Awaitable[ComponentHealth]]
LifecycleCallable = Callable[[], Awaitable[None]]


class ServiceState(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    RESTARTING = "RESTARTING"
    FAILED = "FAILED"


@dataclass
class ServiceRecord:
    name: str
    critical: bool
    max_restarts: int
    state: ServiceState = ServiceState.STOPPED
    restart_count: int = 0
    last_error: str = ""
    last_checked_at: float = 0.0
    last_latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "critical": self.critical,
            "state": str(self.state),
            "restart_count": self.restart_count,
            "max_restarts": self.max_restarts,
            "last_error": self.last_error,
            "last_checked_at": self.last_checked_at,
            "last_latency_ms": self.last_latency_ms,
        }


class ServiceManager:
    """Bounded lifecycle supervision for backend components."""

    def __init__(self) -> None:
        self._services: dict[str, dict[str, Any]] = {}

    def register(
        self,
        name: str,
        *,
        health: HealthCallable,
        restart: LifecycleCallable | None = None,
        start: LifecycleCallable | None = None,
        stop: LifecycleCallable | None = None,
        critical: bool = False,
        max_restarts: int = 3,
    ) -> None:
        self._services[name] = {
            "record": ServiceRecord(
                name=name,
                critical=critical,
                max_restarts=max(0, int(max_restarts)),
            ),
            "health": health,
            "restart": restart,
            "start": start,
            "stop": stop,
        }

    async def start_all(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for name in self._services:
            results.append(await self.start(name))
        return results

    async def start(self, name: str) -> dict[str, Any]:
        service = self._services[name]
        record: ServiceRecord = service["record"]
        record.state = ServiceState.STARTING
        try:
            if service["start"] is not None:
                await service["start"]()
            record.state = ServiceState.RUNNING
            record.last_error = ""
        except Exception as exc:
            record.state = ServiceState.FAILED
            record.last_error = f"{type(exc).__name__}: {exc}"
        return record.to_dict()

    async def stop(self, name: str) -> dict[str, Any]:
        service = self._services[name]
        record: ServiceRecord = service["record"]
        try:
            if service["stop"] is not None:
                await service["stop"]()
            record.state = ServiceState.STOPPED
            record.last_error = ""
        except Exception as exc:
            record.state = ServiceState.FAILED
            record.last_error = f"{type(exc).__name__}: {exc}"
        return record.to_dict()

    async def restart(self, name: str) -> dict[str, Any]:
        service = self._services[name]
        record: ServiceRecord = service["record"]
        if record.restart_count >= record.max_restarts:
            record.state = ServiceState.FAILED
            record.last_error = "Maximum restart count reached."
            return record.to_dict()
        record.state = ServiceState.RESTARTING
        record.restart_count += 1
        try:
            if service["stop"] is not None:
                await service["stop"]()
            if service["restart"] is not None:
                await service["restart"]()
            elif service["start"] is not None:
                await service["start"]()
            record.state = ServiceState.RUNNING
            record.last_error = ""
        except Exception as exc:
            record.state = ServiceState.FAILED
            record.last_error = f"{type(exc).__name__}: {exc}"
        return record.to_dict()

    async def health_check(self, name: str) -> dict[str, Any]:
        service = self._services[name]
        record: ServiceRecord = service["record"]
        started = time.perf_counter()
        try:
            health = await service["health"]()
        except Exception as exc:
            health = ComponentHealth(
                name=name,
                status=HealthStatus.ERROR,
                message=f"{type(exc).__name__}: {exc}",
            )
        record.last_checked_at = time.time()
        record.last_latency_ms = round((time.perf_counter() - started) * 1_000, 3)
        if health.status == HealthStatus.HEALTHY:
            record.state = ServiceState.RUNNING
        elif health.status == HealthStatus.DEGRADED:
            record.state = ServiceState.DEGRADED
        else:
            record.state = ServiceState.FAILED
        record.last_error = health.message
        return {
            **record.to_dict(),
            "health": health.to_dict(),
        }

    async def check_all(self, *, auto_restart: bool = False) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for name in self._services:
            result = await self.health_check(name)
            if auto_restart and result["state"] == str(ServiceState.FAILED):
                if not result["critical"] or result["restart_count"] < result["max_restarts"]:
                    result = await self.restart(name)
            results.append(result)
        return results

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            service["record"].to_dict()
            for service in self._services.values()
        ]

    def reset_restart_counts(self) -> None:
        for service in self._services.values():
            service["record"].restart_count = 0
