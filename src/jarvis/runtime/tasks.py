from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any

from jarvis.runtime.events import EventBus, EventType, RuntimeEvent


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"


@dataclass(frozen=True)
class TaskStep:
    id: str
    title: str
    tool: str = ""
    status: TaskStatus = TaskStatus.CREATED
    result: str = ""
    error: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "tool": self.tool,
            "status": str(self.status),
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TaskStep:
        return cls(
            id=str(value.get("id") or uuid.uuid4().hex),
            title=str(value.get("title") or ""),
            tool=str(value.get("tool") or ""),
            status=TaskStatus(str(value.get("status") or TaskStatus.CREATED)),
            result=str(value.get("result") or ""),
            error=str(value.get("error") or ""),
            started_at=value.get("started_at"),
            completed_at=value.get("completed_at"),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True)
class Task:
    id: str
    goal: str
    status: TaskStatus
    plan: list[TaskStep]
    tools: list[str]
    priority: int
    progress: float
    result: str
    error: str
    session_id: str | None
    created_at: str
    updated_at: str

    def with_updates(self, **changes: Any) -> Task:
        values = {
            "id": self.id,
            "goal": self.goal,
            "status": self.status,
            "plan": self.plan,
            "tools": self.tools,
            "priority": self.priority,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        values.update(changes)
        return Task(**values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "status": str(self.status),
            "plan": [step.to_dict() for step in self.plan],
            "tools": self.tools,
            "priority": self.priority,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    tools_json TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 3,
                    progress REAL NOT NULL DEFAULT 0,
                    result TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    session_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_updated
                    ON tasks(updated_at DESC);
                """
            )

    def create(
        self,
        goal: str,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        priority: int = 3,
        plan: list[TaskStep] | None = None,
        tools: list[str] | None = None,
    ) -> Task:
        now = utc_now()
        task = Task(
            id=task_id or uuid.uuid4().hex,
            goal=goal.strip(),
            status=TaskStatus.CREATED,
            plan=plan or [],
            tools=tools or [],
            priority=max(1, min(int(priority), 5)),
            progress=0.0,
            result="",
            error="",
            session_id=session_id,
            created_at=now,
            updated_at=now,
        )
        self.save(task)
        return task

    def save(self, task: Task) -> Task:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO tasks(
                    id, goal, status, plan_json, tools_json, priority,
                    progress, result, error, session_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    goal = excluded.goal,
                    status = excluded.status,
                    plan_json = excluded.plan_json,
                    tools_json = excluded.tools_json,
                    priority = excluded.priority,
                    progress = excluded.progress,
                    result = excluded.result,
                    error = excluded.error,
                    session_id = excluded.session_id,
                    updated_at = excluded.updated_at
                """,
                (
                    task.id,
                    task.goal,
                    str(task.status),
                    json.dumps([step.to_dict() for step in task.plan], ensure_ascii=False),
                    json.dumps(task.tools, ensure_ascii=False),
                    task.priority,
                    task.progress,
                    task.result,
                    task.error,
                    task.session_id,
                    task.created_at,
                    task.updated_at,
                ),
            )
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._row_to_task(row) if row else None

    def list(self, limit: int = 50) -> list[Task]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def latest(self) -> Task | None:
        tasks = self.list(1)
        return tasks[0] if tasks else None

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        plan_data = json.loads(str(row["plan_json"]))
        return Task(
            id=str(row["id"]),
            goal=str(row["goal"]),
            status=TaskStatus(str(row["status"])),
            plan=[TaskStep.from_dict(item) for item in plan_data],
            tools=list(json.loads(str(row["tools_json"]))),
            priority=int(row["priority"]),
            progress=float(row["progress"]),
            result=str(row["result"]),
            error=str(row["error"]),
            session_id=str(row["session_id"]) if row["session_id"] is not None else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


class TaskEngine:
    """Durable task state machine with runtime event emission."""

    def __init__(self, store: TaskStore, event_bus: EventBus | None = None) -> None:
        self.store = store
        self.event_bus = event_bus or EventBus()

    async def create_task(
        self,
        goal: str,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        priority: int = 3,
        plan: list[TaskStep] | None = None,
        tools: list[str] | None = None,
    ) -> Task:
        task = self.store.create(
            goal,
            task_id=task_id,
            session_id=session_id,
            priority=priority,
            plan=plan,
            tools=tools,
        )
        await self._emit(EventType.TASK_START, task)
        return task

    async def set_plan(self, task_id: str, steps: list[TaskStep]) -> Task:
        task = self._require(task_id)
        updated = task.with_updates(
            plan=steps,
            status=TaskStatus.PLANNING,
            tools=sorted({step.tool for step in steps if step.tool}),
            updated_at=utc_now(),
        )
        self.store.save(updated)
        await self._emit(EventType.PLAN_CREATED, updated)
        return updated

    async def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result: str | None = None,
        error: str | None = None,
        progress: float | None = None,
    ) -> Task:
        task = self._require(task_id)
        updated = task.with_updates(
            status=status,
            result=result if result is not None else task.result,
            error=error if error is not None else task.error,
            progress=max(0.0, min(progress if progress is not None else task.progress, 1.0)),
            updated_at=utc_now(),
        )
        self.store.save(updated)
        await self._emit(EventType.PLAN_CREATED, updated)
        return updated

    async def start_step(self, task_id: str, step_id: str) -> Task:
        return await self._update_step(task_id, step_id, status=TaskStatus.EXECUTING)

    async def complete_step(self, task_id: str, step_id: str, result: str = "") -> Task:
        return await self._update_step(
            task_id,
            step_id,
            status=TaskStatus.COMPLETED,
            result=result,
            completed=True,
        )

    async def fail_step(self, task_id: str, step_id: str, error: str) -> Task:
        return await self._update_step(
            task_id,
            step_id,
            status=TaskStatus.FAILED,
            error=error,
            completed=True,
        )

    async def complete_task(self, task_id: str, result: str = "") -> Task:
        task = await self.update_status(
            task_id,
            TaskStatus.COMPLETED,
            result=result,
            progress=1.0,
        )
        await self._emit(EventType.TASK_COMPLETE, task)
        return task

    async def fail_task(self, task_id: str, error: str) -> Task:
        task = await self.update_status(task_id, TaskStatus.FAILED, error=error)
        await self._emit(EventType.TASK_FAILED, task)
        return task

    async def cancel_task(self, task_id: str) -> Task:
        task = await self.update_status(task_id, TaskStatus.CANCELLED)
        await self._emit(EventType.TASK_CANCELLED, task)
        return task

    async def pause_task(self, task_id: str) -> Task:
        return await self.update_status(task_id, TaskStatus.PAUSED)

    async def resume_task(self, task_id: str) -> Task:
        return await self.update_status(task_id, TaskStatus.EXECUTING)

    async def retry_task(self, task_id: str) -> Task:
        return await self.update_status(
            task_id,
            TaskStatus.CREATED,
            error="",
            progress=0.0,
        )

    def get_task(self, task_id: str) -> Task | None:
        return self.store.get(task_id)

    def list_tasks(self, limit: int = 50) -> list[Task]:
        return self.store.list(limit)

    def latest_task(self) -> Task | None:
        return self.store.latest()

    async def _update_step(
        self,
        task_id: str,
        step_id: str,
        *,
        status: TaskStatus,
        result: str = "",
        error: str = "",
        completed: bool = False,
    ) -> Task:
        task = self._require(task_id)
        now = utc_now()
        found = False
        steps: list[TaskStep] = []
        for step in task.plan:
            if step.id != step_id:
                steps.append(step)
                continue
            found = True
            steps.append(
                TaskStep(
                    id=step.id,
                    title=step.title,
                    tool=step.tool,
                    status=status,
                    result=result or step.result,
                    error=error or step.error,
                    started_at=now if status == TaskStatus.EXECUTING else step.started_at,
                    completed_at=now if completed else step.completed_at,
                    metadata=step.metadata,
                )
            )
        if not found:
            raise KeyError(f"Unknown task step: {step_id}")

        completed_steps = sum(1 for step in steps if step.status == TaskStatus.COMPLETED)
        updated = task.with_updates(
            plan=steps,
            progress=completed_steps / max(len(steps), 1),
            status=TaskStatus.EXECUTING,
            updated_at=now,
        )
        self.store.save(updated)
        return updated

    def _require(self, task_id: str) -> Task:
        task = self.store.get(task_id)
        if task is None:
            raise KeyError(f"Unknown task: {task_id}")
        return task

    async def _emit(self, event_type: EventType, task: Task) -> None:
        await self.event_bus.emit_async(
            RuntimeEvent(
                type=event_type,
                task_id=task.id,
                data={
                    "goal": task.goal,
                    "status": str(task.status),
                    "progress": task.progress,
                    "plan": [step.to_dict() for step in task.plan],
                },
            )
        )
