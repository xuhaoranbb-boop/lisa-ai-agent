from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from jarvis.runtime.events import EventBus, RuntimeEvent


@dataclass(frozen=True)
class SystemSnapshot:
    timestamp: float
    cpu_count: int | None = None
    memory_percent: float | None = None
    disk_free_gb: float | None = None
    services: dict[str, bool] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "cpu_count": self.cpu_count,
            "memory_percent": self.memory_percent,
            "disk_free_gb": self.disk_free_gb,
            "services": self.services,
            "raw": self.raw,
        }


class SystemMonitor:
    def __init__(
        self,
        collector: Any,
        event_bus: EventBus,
        *,
        interval_seconds: float = 30.0,
        memory_warning_percent: float = 90.0,
        disk_warning_gb: float = 5.0,
    ) -> None:
        self.collector = collector
        self.event_bus = event_bus
        self.interval_seconds = max(2.0, interval_seconds)
        self.memory_warning_percent = memory_warning_percent
        self.disk_warning_gb = disk_warning_gb
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def sample(self) -> SystemSnapshot:
        raw = await self.collector.get_system_info()
        services = {
            "bridge": True,
            "asr": True,
            "tts": True,
            "llm": True,
            "mcp": True,
        }
        return SystemSnapshot(
            timestamp=time.time(),
            cpu_count=int(raw.get("cpu_count") or 0) or None,
            memory_percent=float(raw.get("memory_percent") or 0) or None,
            disk_free_gb=float(raw.get("disk_free_gb") or 0) or None,
            services=services,
            raw=raw,
        )

    async def check(self) -> list[dict[str, Any]]:
        snapshot = await self.sample()
        alerts: list[dict[str, Any]] = []
        if (
            snapshot.memory_percent is not None
            and snapshot.memory_percent >= self.memory_warning_percent
        ):
            alerts.append(
                {
                    "type": "memory",
                    "severity": "warning",
                    "message": (
                        f"Memory usage is {snapshot.memory_percent:.0f}%."
                    ),
                }
            )
        if (
            snapshot.disk_free_gb is not None
            and snapshot.disk_free_gb <= self.disk_warning_gb
        ):
            alerts.append(
                {
                    "type": "disk",
                    "severity": "warning",
                    "message": (
                        f"Only {snapshot.disk_free_gb:.1f} GB free disk space remains."
                    ),
                }
            )
        for service, healthy in snapshot.services.items():
            if not healthy:
                alerts.append(
                    {
                        "type": "service",
                        "severity": "error",
                        "message": f"Service is unavailable: {service}",
                    }
                )
        for alert in alerts:
            await self.event_bus.emit_async(
                RuntimeEvent(
                    type="SYSTEM_ALERT",
                    data={"alert": alert, "snapshot": snapshot.to_dict()},
                )
            )
        return alerts

    async def _loop(self) -> None:
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.interval_seconds)
