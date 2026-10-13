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

from jarvis.runtime.cancellation import CancellationToken
from jarvis.runtime.events import EventBus, RuntimeEvent
from jarvis.runtime.execution import AgentExecutionEngine
from jarvis.runtime.tasks import TaskStep


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class WorkflowStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class WorkflowStep:
    id: str
    title: str
    goal: str
    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
    rollback_tool: str = ""
    rollback_arguments: dict[str, Any] = field(default_factory=dict)
    retry_policy: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "tool": self.tool,
            "arguments": self.arguments,
            "dependencies": self.dependencies,
            "rollback_tool": self.rollback_tool,
            "rollback_arguments": self.rollback_arguments,
            "retry_policy": self.retry_policy,
        }


@dataclass
class WorkflowStepRun:
    step_id: str
    status: str = "PENDING"
    result: str = ""
    error: str = ""
    retry_count: int = 0
    execution_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "retry_count": self.retry_count,
            "execution_id": self.execution_id,
        }


@dataclass
class WorkflowRun:
    id: str
    name: str
    session_id: str
    status: WorkflowStatus
    steps: list[WorkflowStep]
    step_runs: dict[str, WorkflowStepRun]
    result: str = ""
    error: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.id,
            "name": self.name,
            "session_id": self.session_id,
            "status": str(self.status),
            "steps": [step.to_dict() for step in self.steps],
            "step_runs": [
                self.step_runs[step.id].to_dict()
                for step in self.steps
                if step.id in self.step_runs
            ],
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class WorkflowStore:
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
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    step_runs_json TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_runs_updated
                    ON workflow_runs(updated_at DESC);
                """
            )

    def save(self, run: WorkflowRun) -> WorkflowRun:
        run.updated_at = _now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO workflow_runs(
                    id, name, session_id, status, steps_json, step_runs_json,
                    result, error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    step_runs_json = excluded.step_runs_json,
                    result = excluded.result,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (
                    run.id,
                    run.name,
                    run.session_id,
                    str(run.status),
                    json.dumps([step.to_dict() for step in run.steps], ensure_ascii=False),
                    json.dumps(
                        {
                            step_id: result.to_dict()
                            for step_id, result in run.step_runs.items()
                        },
                        ensure_ascii=False,
                    ),
                    run.result,
                    run.error,
                    run.created_at,
                    run.updated_at,
                ),
            )
        return run

    def get(self, workflow_id: str) -> WorkflowRun | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM workflow_runs WHERE id = ?",
                (workflow_id,),
            ).fetchone()
        return self._row_to_run(row) if row else None

    def list(self, limit: int = 50) -> list[WorkflowRun]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM workflow_runs ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> WorkflowRun:
        steps = [
            WorkflowStep(
                id=str(item["id"]),
                title=str(item["title"]),
                goal=str(item["goal"]),
                tool=str(item.get("tool") or ""),
                arguments=dict(item.get("arguments") or {}),
                dependencies=list(item.get("dependencies") or []),
                rollback_tool=str(item.get("rollback_tool") or ""),
                rollback_arguments=dict(item.get("rollback_arguments") or {}),
                retry_policy=dict(item.get("retry_policy") or {}),
            )
            for item in json.loads(str(row["steps_json"]))
        ]
        step_runs = {
            step_id: WorkflowStepRun(
                step_id=step_id,
                status=str(item.get("status") or "PENDING"),
                result=str(item.get("result") or ""),
                error=str(item.get("error") or ""),
                retry_count=int(item.get("retry_count") or 0),
                execution_id=str(item.get("execution_id") or ""),
            )
            for step_id, item in json.loads(str(row["step_runs_json"])).items()
        }
        return WorkflowRun(
            id=str(row["id"]),
            name=str(row["name"]),
            session_id=str(row["session_id"]),
            status=WorkflowStatus(str(row["status"])),
            steps=steps,
            step_runs=step_runs,
            result=str(row["result"]),
            error=str(row["error"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


class WorkflowEngine:
    """Execute dependency-ordered multi-agent workflows with rollback."""

    def __init__(
        self,
        *,
        store: WorkflowStore,
        execution_engine: AgentExecutionEngine,
        event_bus: EventBus | None = None,
    ) -> None:
        self.store = store
        self.execution_engine = execution_engine
        self.event_bus = event_bus or EventBus()

    async def create(
        self,
        *,
        name: str,
        session_id: str,
        steps: list[WorkflowStep],
    ) -> WorkflowRun:
        self._validate_dependencies(steps)
        run = WorkflowRun(
            id=uuid.uuid4().hex,
            name=name,
            session_id=session_id,
            status=WorkflowStatus.QUEUED,
            steps=steps,
            step_runs={step.id: WorkflowStepRun(step.id) for step in steps},
        )
        return self.store.save(run)

    async def run(
        self,
        workflow_id: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> WorkflowRun:
        run = self._require(workflow_id)
        cancellation = cancellation or CancellationToken()
        ordered = self._topological_order(run.steps)
        run.status = WorkflowStatus.RUNNING
        self.store.save(run)
        completed: list[WorkflowStep] = []
        try:
            for step in ordered:
                cancellation.raise_if_cancelled()
                step_run = run.step_runs[step.id]
                step_run.status = "RUNNING"
                self.store.save(run)
                try:
                    execution = await self.execution_engine.run(
                        step.goal,
                        session_id=run.session_id,
                        plan=[
                            TaskStep(
                                id=step.id,
                                title=step.title,
                                tool=step.tool,
                                metadata={"arguments": step.arguments},
                            )
                        ],
                    )
                    step_run.execution_id = execution.id
                    if execution.status.value != "COMPLETED":
                        raise RuntimeError(execution.error or "Workflow step failed.")
                    step_run.status = "COMPLETED"
                    step_run.result = execution.result
                    completed.append(step)
                except Exception as exc:
                    step_run.error = f"{type(exc).__name__}: {exc}"
                    step_run.retry_count += 1
                    step_run.status = "FAILED"
                    await self._rollback(run, completed)
                    raise
                finally:
                    self.store.save(run)
            run.status = WorkflowStatus.COMPLETED
            run.result = f"Completed {len(ordered)} workflow steps."
            self.store.save(run)
            return run
        except Exception as exc:
            run.status = WorkflowStatus.FAILED
            run.error = f"{type(exc).__name__}: {exc}"
            self.store.save(run)
            raise

    async def _rollback(
        self,
        run: WorkflowRun,
        completed: list[WorkflowStep],
    ) -> None:
        for step in reversed(completed):
            if not step.rollback_tool:
                continue
            try:
                await self.execution_engine.registry.call(
                    step.rollback_tool,
                    step.rollback_arguments,
                    permission_manager=self.execution_engine.permission_manager,
                )
                run.step_runs[step.id].status = "ROLLED_BACK"
            except Exception as exc:
                run.step_runs[step.id].error += (
                    f"\nRollback failed: {type(exc).__name__}: {exc}"
                )
        self.store.save(run)

    def _require(self, workflow_id: str) -> WorkflowRun:
        run = self.store.get(workflow_id)
        if run is None:
            raise KeyError(f"Unknown workflow: {workflow_id}")
        return run

    @staticmethod
    def _validate_dependencies(steps: list[WorkflowStep]) -> None:
        ids = {step.id for step in steps}
        for step in steps:
            missing = set(step.dependencies) - ids
            if missing:
                raise ValueError(
                    f"Step {step.id} references missing dependencies: {sorted(missing)}"
                )
        WorkflowEngine._topological_order(steps)

    @staticmethod
    def _topological_order(steps: list[WorkflowStep]) -> list[WorkflowStep]:
        by_id = {step.id: step for step in steps}
        ordered: list[WorkflowStep] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visited:
                return
            if step_id in visiting:
                raise ValueError("Workflow contains a dependency cycle.")
            visiting.add(step_id)
            for dependency in by_id[step_id].dependencies:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)
            ordered.append(by_id[step_id])

        for step in steps:
            visit(step.id)
        return ordered

    async def _emit(self, event_type: str, run: WorkflowRun) -> None:
        await self.event_bus.emit_async(
            RuntimeEvent(
                type=event_type,
                data={"workflow_id": run.id, "workflow": run.to_dict()},
            )
        )
