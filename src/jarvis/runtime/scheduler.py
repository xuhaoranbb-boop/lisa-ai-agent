from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from jarvis.runtime.events import EventBus, EventType, RuntimeEvent

JobFactory = Callable[[], Awaitable[Any]]


@dataclass
class ScheduledJob:
    id: str
    name: str
    factory: JobFactory
    interval_seconds: float | None = None
    next_run_at: float = field(default_factory=time.monotonic)
    status: str = "SCHEDULED"
    run_count: int = 0
    last_result: Any = None
    last_error: str = ""
    notify_on_complete: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "interval_seconds": self.interval_seconds,
            "next_run_at": self.next_run_at,
            "status": self.status,
            "run_count": self.run_count,
            "last_result": self.last_result,
            "last_error": self.last_error,
        }


class TaskScheduler:
    """Small asyncio scheduler for proactive and background JARVIS work."""

    def __init__(self, event_bus: EventBus | None = None) -> None:
        self.event_bus = event_bus or EventBus()
        self._jobs: dict[str, ScheduledJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running = False
        self._loop_task: asyncio.Task[None] | None = None

    def add_interval(
        self,
        name: str,
        factory: JobFactory,
        *,
        interval_seconds: float,
        start_after_seconds: float = 0.0,
    ) -> ScheduledJob:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive.")
        job = ScheduledJob(
            id=uuid.uuid4().hex,
            name=name,
            factory=factory,
            interval_seconds=interval_seconds,
            next_run_at=time.monotonic() + max(0.0, start_after_seconds),
        )
        self._jobs[job.id] = job
        return job

    def add_once(
        self,
        name: str,
        factory: JobFactory,
        *,
        run_after_seconds: float = 0.0,
        notify_on_complete: bool = True,
    ) -> ScheduledJob:
        job = ScheduledJob(
            id=uuid.uuid4().hex,
            name=name,
            factory=factory,
            interval_seconds=None,
            next_run_at=time.monotonic() + max(0.0, run_after_seconds),
            notify_on_complete=notify_on_complete,
        )
        self._jobs[job.id] = job
        return job

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.status = "CANCELLED"
        task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()
        return True

    def pause(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.status = "PAUSED"
        return True

    def resume(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.status = "SCHEDULED"
        job.next_run_at = time.monotonic()
        return True

    def jobs(self) -> list[ScheduledJob]:
        return list(self._jobs.values())

    async def run_pending(self) -> int:
        now = time.monotonic()
        pending = [
            job
            for job in self._jobs.values()
            if job.status == "SCHEDULED" and job.next_run_at <= now
        ]
        for job in pending:
            self._tasks[job.id] = asyncio.create_task(self._run_job(job))
        return len(pending)

    async def _run_loop(self) -> None:
        while self._running:
            await self.run_pending()
            await asyncio.sleep(0.05)

    async def _run_job(self, job: ScheduledJob) -> None:
        if job.status == "CANCELLED":
            return
        job.status = "RUNNING"
        await self._emit(
            EventType.BACKGROUND_TASK_STARTED,
            {"job_id": job.id, "name": job.name},
        )
        try:
            result = await job.factory()
        except asyncio.CancelledError:
            job.status = "CANCELLED"
            await self._emit(
                EventType.BACKGROUND_TASK_CANCELLED,
                {"job_id": job.id, "name": job.name},
            )
            raise
        except Exception as exc:
            job.status = "FAILED"
            job.last_error = str(exc)
            await self._emit(
                EventType.BACKGROUND_TASK_FAILED,
                {"job_id": job.id, "name": job.name, "error": str(exc)},
            )
        else:
            job.run_count += 1
            job.last_result = result
            if job.interval_seconds is None:
                job.status = "COMPLETED"
            else:
                job.status = "SCHEDULED"
                job.next_run_at = time.monotonic() + job.interval_seconds
            if job.notify_on_complete:
                await self._emit(
                    EventType.BACKGROUND_TASK_COMPLETED,
                    {"job_id": job.id, "name": job.name, "result": result},
                )
        finally:
            self._tasks.pop(job.id, None)

    async def _emit(self, event_type: EventType, data: dict[str, Any]) -> None:
        await self.event_bus.emit_async(RuntimeEvent(type=event_type, data=data))
