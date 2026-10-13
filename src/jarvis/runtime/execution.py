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

from jarvis.runtime.budget import TokenBudgetManager, estimate_tokens
from jarvis.runtime.cancellation import CancellationToken, OperationCancelled
from jarvis.runtime.context import ContextEngine
from jarvis.runtime.events import EventBus, EventType, RuntimeEvent
from jarvis.runtime.permissions import PermissionManager
from jarvis.runtime.planner import Planner
from jarvis.runtime.tasks import TaskEngine, TaskStatus, TaskStep
from jarvis.runtime.tooling import ToolRegistry


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ExecutionStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class RecoveryStrategy(StrEnum):
    RETRY = "RETRY"
    REPLAN = "REPLAN"
    FALLBACK = "FALLBACK"
    ASK_USER = "ASK_USER"
    ABORT = "ABORT"


@dataclass
class ExecutionStepRecord:
    step_id: str
    title: str
    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    status: str = "PENDING"
    result: str = ""
    error: str = ""
    retry_count: int = 0
    started_at: str | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "tool": self.tool,
            "arguments": self.arguments,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "retry_count": self.retry_count,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@dataclass
class ExecutionRecord:
    id: str
    task_id: str
    goal: str
    session_id: str
    status: ExecutionStatus
    current_step: str
    result: str
    error: str
    input_tokens: int
    output_tokens: int
    embedding_tokens: int
    estimated_cost: float
    steps: list[ExecutionStepRecord]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.id,
            "task_id": self.task_id,
            "goal": self.goal,
            "session_id": self.session_id,
            "status": str(self.status),
            "current_step": self.current_step,
            "result": self.result,
            "error": self.error,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "embedding_tokens": self.embedding_tokens,
            "estimated_cost": self.estimated_cost,
            "steps": [step.to_dict() for step in self.steps],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ExecutionStore:
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
                CREATE TABLE IF NOT EXISTS execution_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step TEXT NOT NULL DEFAULT '',
                    result TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    embedding_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_steps (
                    execution_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    tool TEXT NOT NULL DEFAULT '',
                    arguments_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT,
                    completed_at TEXT,
                    PRIMARY KEY(execution_id, step_id),
                    FOREIGN KEY(execution_id) REFERENCES execution_runs(id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_execution_runs_updated
                    ON execution_runs(updated_at DESC);
                """
            )
            existing = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(execution_runs)"
                ).fetchall()
            }
            for name, definition in {
                "input_tokens": "INTEGER NOT NULL DEFAULT 0",
                "output_tokens": "INTEGER NOT NULL DEFAULT 0",
                "embedding_tokens": "INTEGER NOT NULL DEFAULT 0",
                "estimated_cost": "REAL NOT NULL DEFAULT 0",
            }.items():
                if name not in existing:
                    self._connection.execute(
                        f"ALTER TABLE execution_runs ADD COLUMN {name} {definition}"
                    )

    def create(
        self,
        *,
        execution_id: str | None = None,
        task_id: str,
        goal: str,
        session_id: str,
        plan: list[TaskStep],
    ) -> ExecutionRecord:
        now = _utc_now()
        record = ExecutionRecord(
            id=execution_id or uuid.uuid4().hex,
            task_id=task_id,
            goal=goal,
            session_id=session_id,
            status=ExecutionStatus.QUEUED,
            current_step="",
            result="",
            error="",
            input_tokens=0,
            output_tokens=0,
            embedding_tokens=0,
            estimated_cost=0.0,
            steps=[
                ExecutionStepRecord(
                    step_id=step.id,
                    title=step.title,
                    tool=step.tool,
                    arguments=dict(step.metadata.get("arguments") or {}),
                )
                for step in plan
            ],
            created_at=now,
            updated_at=now,
        )
        self.save(record)
        return record

    def save(self, record: ExecutionRecord) -> ExecutionRecord:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO execution_runs(
                    id, task_id, goal, session_id, status, current_step,
                    result, error, created_at, updated_at
                    , input_tokens, output_tokens, embedding_tokens, estimated_cost
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    current_step = excluded.current_step,
                    result = excluded.result,
                    error = excluded.error,
                    input_tokens = excluded.input_tokens,
                    output_tokens = excluded.output_tokens,
                    embedding_tokens = excluded.embedding_tokens,
                    estimated_cost = excluded.estimated_cost,
                    updated_at = excluded.updated_at
                """,
                (
                    record.id,
                    record.task_id,
                    record.goal,
                    record.session_id,
                    str(record.status),
                    record.current_step,
                    record.result,
                    record.error,
                    record.created_at,
                    record.updated_at,
                    record.input_tokens,
                    record.output_tokens,
                    record.embedding_tokens,
                    record.estimated_cost,
                ),
            )
            for index, step in enumerate(record.steps):
                self._connection.execute(
                    """
                    INSERT INTO execution_steps(
                        execution_id, step_id, ordinal, title, tool,
                        arguments_json, status, result, error, retry_count,
                        started_at, completed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(execution_id, step_id) DO UPDATE SET
                        ordinal = excluded.ordinal,
                        title = excluded.title,
                        tool = excluded.tool,
                        arguments_json = excluded.arguments_json,
                        status = excluded.status,
                        result = excluded.result,
                        error = excluded.error,
                        retry_count = excluded.retry_count,
                        started_at = excluded.started_at,
                        completed_at = excluded.completed_at
                    """,
                    (
                        record.id,
                        step.step_id,
                        index,
                        step.title,
                        step.tool,
                        json.dumps(step.arguments, ensure_ascii=False, default=str),
                        step.status,
                        step.result,
                        step.error,
                        step.retry_count,
                        step.started_at,
                        step.completed_at,
                    ),
                )
        return record

    def get(self, execution_id: str) -> ExecutionRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM execution_runs WHERE id = ?",
                (execution_id,),
            ).fetchone()
            step_rows = self._connection.execute(
                """
                SELECT *
                FROM execution_steps
                WHERE execution_id = ?
                ORDER BY ordinal ASC
                """,
                (execution_id,),
            ).fetchall()
        if row is None:
            return None
        return self._row_to_record(row, step_rows)

    def list(self, limit: int = 50) -> list[ExecutionRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM execution_runs ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            records: list[ExecutionRecord] = []
            for row in rows:
                step_rows = self._connection.execute(
                    """
                    SELECT *
                    FROM execution_steps
                    WHERE execution_id = ?
                    ORDER BY ordinal ASC
                    """,
                    (str(row["id"]),),
                ).fetchall()
                records.append(self._row_to_record(row, step_rows))
        return records

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _row_to_record(
        row: sqlite3.Row,
        step_rows: list[sqlite3.Row],
    ) -> ExecutionRecord:
        return ExecutionRecord(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            goal=str(row["goal"]),
            session_id=str(row["session_id"]),
            status=ExecutionStatus(str(row["status"])),
            current_step=str(row["current_step"]),
            result=str(row["result"]),
            error=str(row["error"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            embedding_tokens=int(row["embedding_tokens"]),
            estimated_cost=float(row["estimated_cost"]),
            steps=[
                ExecutionStepRecord(
                    step_id=str(step["step_id"]),
                    title=str(step["title"]),
                    tool=str(step["tool"]),
                    arguments=dict(json.loads(str(step["arguments_json"] or "{}"))),
                    status=str(step["status"]),
                    result=str(step["result"]),
                    error=str(step["error"]),
                    retry_count=int(step["retry_count"]),
                    started_at=(
                        str(step["started_at"])
                        if step["started_at"] is not None
                        else None
                    ),
                    completed_at=(
                        str(step["completed_at"])
                        if step["completed_at"] is not None
                        else None
                    ),
                )
                for step in step_rows
            ],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


@dataclass(frozen=True)
class ExecutionObservation:
    success: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationResult:
    success: bool
    reason: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryDecision:
    strategy: RecoveryStrategy
    reason: str


class ObservationEngine:
    async def observe(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> ExecutionObservation:
        if isinstance(result, dict) and result.get("ok") is False:
            return ExecutionObservation(
                success=False,
                detail=str(result.get("error") or "Tool returned failure."),
                data={"result": result},
            )
        path = arguments.get("path")
        if isinstance(path, str) and path:
            target = Path(path).expanduser()
            return ExecutionObservation(
                success=target.exists(),
                detail=f"Path exists: {target}" if target.exists() else f"Path missing: {target}",
                data={"path": str(target), "exists": target.exists()},
            )
        return ExecutionObservation(
            success=True,
            detail="Tool returned without an exception.",
            data={"result": result},
        )


class VerificationEngine:
    def verify(
        self,
        step: ExecutionStepRecord,
        observation: ExecutionObservation,
    ) -> VerificationResult:
        expected = step.arguments.get("expected")
        if expected is not None and observation.data.get("result") != expected:
            return VerificationResult(
                success=False,
                reason="Observed result did not match the expected value.",
                data=observation.data,
            )
        return VerificationResult(
            success=observation.success,
            reason=observation.detail,
            data=observation.data,
        )


class RecoveryEngine:
    def decide(
        self,
        error: str,
        *,
        retry_count: int,
        max_retries: int = 2,
        replanned: bool = False,
    ) -> RecoveryDecision:
        lowered = error.lower()
        if "permissiondenied" in lowered or "permission" in lowered or "not allowed" in lowered:
            return RecoveryDecision(
                RecoveryStrategy.ASK_USER,
                "Permission denied.",
            )
        if "schema" in lowered or "argument" in lowered or "invalid" in lowered:
            return RecoveryDecision(
                RecoveryStrategy.REPLAN,
                "Invalid arguments require a new plan.",
            )
        if retry_count < max_retries:
            return RecoveryDecision(
                RecoveryStrategy.RETRY,
                f"Transient failure; retry {retry_count + 1}/{max_retries}.",
            )
        if not replanned:
            return RecoveryDecision(
                RecoveryStrategy.REPLAN,
                "Retry budget exhausted; replanning once.",
            )
        return RecoveryDecision(
            RecoveryStrategy.ABORT,
            "Retry and replan budgets exhausted.",
        )


class AgentExecutionEngine:
    """Bounded understand-plan-execute-observe-verify-recover lifecycle."""

    def __init__(
        self,
        *,
        store: ExecutionStore,
        registry: ToolRegistry,
        permission_manager: PermissionManager,
        planner: Planner,
        task_engine: TaskEngine,
        context_engine: ContextEngine | None = None,
        event_bus: EventBus | None = None,
        provider: Any | None = None,
        observation_engine: ObservationEngine | None = None,
        verification_engine: VerificationEngine | None = None,
        recovery_engine: RecoveryEngine | None = None,
        max_steps: int = 20,
        max_retries: int = 2,
        budget_manager: TokenBudgetManager | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.permission_manager = permission_manager
        self.planner = planner
        self.task_engine = task_engine
        self.context_engine = context_engine
        self.event_bus = event_bus or EventBus()
        self.provider = provider
        self.observation_engine = observation_engine or ObservationEngine()
        self.verification_engine = verification_engine or VerificationEngine()
        self.recovery_engine = recovery_engine or RecoveryEngine()
        self.max_steps = max(1, max_steps)
        self.max_retries = max(0, max_retries)
        self.budget_manager = budget_manager or TokenBudgetManager()

    async def run(
        self,
        goal: str,
        *,
        session_id: str,
        plan: list[TaskStep] | None = None,
        task_id: str | None = None,
        cancellation: CancellationToken | None = None,
        execution_id: str | None = None,
    ) -> ExecutionRecord:
        cancellation = cancellation or CancellationToken()
        task = self.task_engine.get_task(task_id) if task_id else None
        if task is None:
            task = await self.task_engine.create_task(
                goal,
                session_id=session_id,
                task_id=task_id,
                tools=[],
            )
        task_id = task.id
        preliminary_steps = plan or [
            TaskStep(id=uuid.uuid4().hex, title=goal)
        ]
        record = self.store.create(
            execution_id=execution_id,
            task_id=task.id,
            goal=goal,
            session_id=session_id,
            plan=preliminary_steps,
        )
        record.status = ExecutionStatus.RUNNING
        self.store.save(record)
        await self._emit(EventType.EXECUTION_STARTED, record)

        context_text = ""
        if self.context_engine is not None:
            context = await self.context_engine.build_context(
                goal,
                session_id=session_id,
                task_id=task_id,
            )
            context_text = context.summary
        steps = plan or await self.planner.create_plan(goal, context_text)
        if not steps:
            steps = [TaskStep(id=uuid.uuid4().hex, title=goal)]
        task = await self.task_engine.set_plan(task.id, steps)
        record.steps = [
            ExecutionStepRecord(
                step_id=item.id,
                title=item.title,
                tool=item.tool,
                arguments=dict(item.metadata.get("arguments") or {}),
            )
            for item in steps
        ]
        self.store.save(record)

        index = 0
        iterations = 0
        replanned = False
        try:
            while index < len(record.steps):
                cancellation.raise_if_cancelled()
                estimated = estimate_tokens(f"{goal}\n{record.steps[index].title}")
                if not self.budget_manager.can_spend(
                    estimated,
                    task_id=record.task_id,
                    request_id=record.id,
                ):
                    raise RuntimeError("Token budget exhausted for this execution.")
                iterations += 1
                if iterations > self.max_steps:
                    raise RuntimeError("Execution exceeded max_steps.")
                step = record.steps[index]
                record.current_step = step.step_id
                step.status = "RUNNING"
                step.started_at = step.started_at or _utc_now()
                task_step_id = self._task_step_id(task.id, step)
                if task_step_id:
                    await self.task_engine.start_step(task.id, task_step_id)
                self.store.save(record)
                await self._emit(EventType.EXECUTION_STEP, record, step=step)
                try:
                    tool_name, arguments = await self._resolve_tool(step, goal, context_text)
                    step.tool = tool_name
                    step.arguments = arguments
                    result = await self.registry.call(
                        tool_name,
                        arguments,
                        permission_manager=self.permission_manager,
                        cancellation=cancellation,
                    )
                    usage = self.budget_manager.record(
                        input_text=f"{goal}\n{step.title}",
                        output_text=json.dumps(result, ensure_ascii=False, default=str),
                        task_id=record.task_id,
                        request_id=record.id,
                        session_id=record.session_id,
                        model=getattr(self.provider, "name", ""),
                    )
                    record.input_tokens += usage.input_tokens
                    record.output_tokens += usage.output_tokens
                    record.embedding_tokens += usage.embedding_tokens
                    record.estimated_cost += usage.estimated_cost
                    await self._emit(
                        EventType.TOKEN_BUDGET,
                        record,
                        step=step,
                        data={"usage": usage.to_dict()},
                    )
                    if self.context_engine is not None:
                        self.context_engine.record_tool_result(
                            record.session_id,
                            tool_name,
                            result,
                        )
                    observation = await self.observation_engine.observe(
                        tool_name,
                        arguments,
                        result,
                    )
                    await self._emit(
                        EventType.EXECUTION_OBSERVATION,
                        record,
                        step=step,
                        data=observation.data,
                    )
                    verification = self.verification_engine.verify(step, observation)
                    await self._emit(
                        EventType.EXECUTION_VERIFIED,
                        record,
                        step=step,
                        data=verification.data,
                    )
                    if not verification.success:
                        raise RuntimeError(verification.reason)
                    step.status = "COMPLETED"
                    step.result = json.dumps(result, ensure_ascii=False, default=str)
                    step.completed_at = _utc_now()
                    if task_step_id:
                        await self.task_engine.complete_step(
                            task.id,
                            task_step_id,
                            step.result,
                        )
                    self.store.save(record)
                    index += 1
                    continue
                except Exception as exc:
                    step.error = f"{type(exc).__name__}: {exc}"
                    decision = self.recovery_engine.decide(
                        step.error,
                        retry_count=step.retry_count,
                        max_retries=self.max_retries,
                        replanned=replanned,
                    )
                    await self._emit(
                        EventType.EXECUTION_RECOVERED,
                        record,
                        step=step,
                        data={
                            "strategy": str(decision.strategy),
                            "reason": decision.reason,
                        },
                    )
                    if decision.strategy == RecoveryStrategy.RETRY:
                        step.retry_count += 1
                        step.status = "RETRYING"
                        self.store.save(record)
                        continue
                    if decision.strategy == RecoveryStrategy.REPLAN and not replanned:
                        replanned = True
                        remaining = await self.planner.create_plan(
                            goal,
                            f"{context_text}\nFailure: {step.error}",
                        )
                        if not remaining:
                            raise RuntimeError("Replanner returned no steps.") from None
                        record.steps = record.steps[:index] + [
                            ExecutionStepRecord(
                                step_id=item.id,
                                title=item.title,
                                tool=item.tool,
                                arguments=dict(item.metadata.get("arguments") or {}),
                            )
                            for item in remaining
                        ]
                        self.store.save(record)
                        continue
                    if decision.strategy == RecoveryStrategy.ASK_USER:
                        record.status = ExecutionStatus.WAITING_CONFIRMATION
                        self.store.save(record)
                        if task_step_id:
                            await self.task_engine.fail_step(
                                task.id,
                                task_step_id,
                                step.error,
                            )
                        await self.task_engine.update_status(
                            task.id,
                            TaskStatus.WAITING_CONFIRMATION,
                        )
                        return record
                    if task_step_id:
                        await self.task_engine.fail_step(
                            task.id,
                            task_step_id,
                            step.error,
                        )
                    raise
            record.status = ExecutionStatus.COMPLETED
            record.result = f"Completed {len(record.steps)} step(s)."
            record.error = ""
            self.store.save(record)
            await self.task_engine.complete_task(task.id, record.result)
            await self._emit(EventType.EXECUTION_COMPLETE, record)
            return record
        except OperationCancelled:
            record.status = ExecutionStatus.CANCELLED
            self.store.save(record)
            await self.task_engine.cancel_task(task.id)
            await self._emit(EventType.EXECUTION_CANCELLED, record)
            raise
        except Exception as exc:
            record.status = ExecutionStatus.FAILED
            record.error = f"{type(exc).__name__}: {exc}"
            self.store.save(record)
            await self.task_engine.fail_task(task.id, record.error)
            await self._emit(EventType.EXECUTION_FAILED, record)
            return record

    async def pause(self, execution_id: str) -> ExecutionRecord:
        record = self._require(execution_id)
        record.status = ExecutionStatus.PAUSED
        self.store.save(record)
        return record

    async def cancel(self, execution_id: str) -> ExecutionRecord:
        record = self._require(execution_id)
        record.status = ExecutionStatus.CANCELLED
        self.store.save(record)
        await self.task_engine.cancel_task(record.task_id)
        await self._emit(EventType.EXECUTION_CANCELLED, record)
        return record

    async def resume(
        self,
        execution_id: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> ExecutionRecord:
        record = self._require(execution_id)
        remaining = [
            TaskStep(
                id=step.step_id,
                title=step.title,
                tool=step.tool,
                metadata={"arguments": step.arguments},
            )
            for step in record.steps
            if step.status != "COMPLETED"
        ]
        return await self.run(
            record.goal,
            session_id=record.session_id,
            plan=remaining,
            task_id=record.task_id,
            cancellation=cancellation,
            execution_id=uuid.uuid4().hex,
        )

    async def _resolve_tool(
        self,
        step: ExecutionStepRecord,
        goal: str,
        context_text: str,
    ) -> tuple[str, dict[str, Any]]:
        if step.tool and self.registry.get(step.tool) is not None:
            return step.tool, dict(step.arguments)
        if self.provider is None:
            raise RuntimeError(f"No registered tool selected for step: {step.title}")
        reply = await self.provider.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Choose exactly one registered tool for the current step. "
                        "Return only JSON: {\"tool\":\"name\",\"arguments\":{}}. "
                        "Never invent a tool name."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Goal: {goal}\nStep: {step.title}\n"
                        f"Context: {context_text}\n"
                        "Tools: "
                        + json.dumps(
                            self.registry.schemas(include_handoff=False),
                            ensure_ascii=False,
                        )
                    ),
                },
            ],
            [],
        )
        try:
            selected = json.loads(reply.content)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Tool selector returned invalid JSON.") from exc
        if not isinstance(selected, dict):
            raise RuntimeError("Tool selector returned a non-object.")
        tool_name = str(selected.get("tool") or "").strip()
        if self.registry.get(tool_name) is None:
            raise RuntimeError(f"Tool selector chose an unregistered tool: {tool_name}")
        arguments = selected.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        return tool_name, arguments

    def _require(self, execution_id: str) -> ExecutionRecord:
        record = self.store.get(execution_id)
        if record is None:
            raise KeyError(f"Unknown execution: {execution_id}")
        return record

    def _task_step_id(self, task_id: str, step: ExecutionStepRecord) -> str | None:
        task = self.task_engine.get_task(task_id)
        if task is None:
            return None
        return next(
            (
                task_step.id
                for task_step in task.plan
                if task_step.id == step.step_id
                or (step.tool and task_step.tool == step.tool)
            ),
            None,
        )

    async def _emit(
        self,
        event_type: EventType,
        record: ExecutionRecord,
        *,
        step: ExecutionStepRecord | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "execution_id": record.id,
            "task_id": record.task_id,
            "goal": record.goal,
            "status": str(record.status),
            "current_step": record.current_step,
            "step": step.to_dict() if step is not None else None,
            **(data or {}),
        }
        await self.event_bus.emit_async(
            RuntimeEvent(
                type=event_type,
                task_id=record.task_id,
                data=payload,
            )
        )
