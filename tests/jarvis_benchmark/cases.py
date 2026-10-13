from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from jarvis.config import Settings
from jarvis.memory import MemoryStore
from jarvis.model import ModelReply
from jarvis.persona import PersonaManager
from jarvis.runtime.budget import TokenBudgetManager
from jarvis.runtime.context import ContextEngine
from jarvis.runtime.contracts import PermissionLevel, ToolSpec
from jarvis.runtime.diagnosis import (
    Diagnosis,
    DiagnosisSeverity,
    SelfRecoveryEngine,
)
from jarvis.runtime.execution import AgentExecutionEngine, ExecutionStore
from jarvis.runtime.permissions import PermissionManager, PermissionPolicy
from jarvis.runtime.semantic_memory import MemoryEmbeddingStore
from jarvis.runtime.tasks import TaskEngine, TaskStep, TaskStore
from jarvis.runtime.tooling import RuntimeTool, ToolRegistry


async def memory_retrieval_case(count: int = 1_000) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="jarvis-bench-memory-") as temporary:
        store = MemoryStore(Path(temporary) / "memory.db")
        try:
            for index in range(count):
                store.remember(
                    f"memory item {index}: preference {index % 20}",
                    category="preference" if index % 2 else "project",
                    importance=(index % 5) + 1,
                    confidence=0.8,
                    owner_id="bench",
                    session_id=f"session-{index % 10}",
                )
            started = asyncio.get_running_loop().time()
            results = store.recall(
                "memory 500",
                10,
                owner_id="bench",
                session_id="session-0",
            )
            latency_ms = (
                asyncio.get_running_loop().time() - started
            ) * 1_000
            return {
                "count": count,
                "results": len(results),
                "retrieval_ms": round(latency_ms, 3),
            }
        finally:
            store.close()


async def semantic_fallback_case() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="jarvis-bench-semantic-") as temporary:
        store = MemoryStore(Path(temporary) / "memory.db")
        try:
            store.remember("project alpha uses Python", owner_id="bench")
            store.remember("project beta uses TypeScript", owner_id="bench")
            semantic = MemoryEmbeddingStore(store, None)
            results = await semantic.search("Python", owner_id="bench")
            return {"results": len(results), "fallback": True}
        finally:
            store.close()


async def token_budget_case() -> dict[str, object]:
    manager = TokenBudgetManager(
        request_token_budget=1_000,
        task_token_budget=5_000,
        daily_token_budget=10_000,
    )
    usage = manager.record(
        input_text="hello " * 100,
        output_text="world " * 100,
        task_id="bench-task",
        session_id="bench-session",
        model="deepseek-chat",
    )
    return {
        "tokens": usage.total_tokens,
        "task_tokens": manager.task_usage("bench-task").total_tokens,
    }


async def security_case() -> dict[str, object]:
    manager = PermissionManager(
        PermissionPolicy(
            auto_approve={
                PermissionLevel.READ_ONLY,
                PermissionLevel.LOW_RISK,
            }
        )
    )
    read = await manager.decide(PermissionLevel.READ_ONLY, "read_file", "file.txt")
    high = await manager.decide(
        PermissionLevel.HIGH_RISK,
        "delete_file",
        "file.txt",
    )
    return {
        "read_allowed": read.allowed,
        "high_allowed_without_confirmation": high.allowed,
    }


async def recovery_case() -> dict[str, object]:
    engine = SelfRecoveryEngine()
    called: list[str] = []

    async def recover(_diagnosis: Diagnosis) -> dict[str, object]:
        called.append("mcp")
        return {"reconnected": True}

    engine.register("mcp", recover)
    result = await engine.recover(
        Diagnosis(
            component="mcp",
            problem="offline",
            severity=DiagnosisSeverity.HIGH,
            likely_cause="disconnect",
            suggested_action="reconnect",
        )
    )
    return {"success": result["success"], "called": called}


class _SummaryProvider:
    async def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        return ModelReply(
            content="Compressed context preserving goals, preferences, and tasks.",
            tool_calls=[],
            raw_message={"role": "assistant", "content": "summary"},
        )


async def context_compression_case() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="jarvis-bench-context-") as temporary:
        database = Path(temporary) / "jarvis.db"
        memory = MemoryStore(database)
        task_store = TaskStore(database)
        task_engine = TaskEngine(task_store)
        try:
            for index in range(20):
                memory.add_message(
                    "bench-context",
                    "user",
                    f"turn {index}: " + "context " * 200,
                )
                memory.add_message(
                    "bench-context",
                    "assistant",
                    f"answer {index}: " + "detail " * 200,
                )
            engine = ContextEngine(
                memory,
                task_engine,
                PersonaManager(
                    Settings(
                        data_dir=Path(temporary) / "data",
                        workspace=Path(temporary),
                    )
                ),
                provider=_SummaryProvider(),
                max_context_chars=3_000,
            )
            snapshot = await engine.build_context(
                "继续这个项目",
                session_id="bench-context",
            )
            return {
                "summary_chars": len(snapshot.summary),
                "history_messages": len(snapshot.history),
            }
        finally:
            memory.close()
            task_store.close()


class _OneStepPlanner:
    async def create_plan(self, goal: str, context: str = "") -> list[TaskStep]:
        return [
            TaskStep(
                id="bench-step",
                title="Return benchmark value",
                tool="benchmark_echo",
                metadata={"arguments": {"value": "ok"}},
            )
        ]


async def agent_execution_case() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="jarvis-bench-agent-") as temporary:
        database = Path(temporary) / "jarvis.db"
        task_store = TaskStore(database)
        execution_store = ExecutionStore(database)
        registry = ToolRegistry()

        async def echo(value: str) -> dict[str, str]:
            return {"value": value}

        registry.register(
            RuntimeTool(
                spec=ToolSpec(
                    name="benchmark_echo",
                    description="Echo benchmark input.",
                    parameters={
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                    permission=PermissionLevel.LOW_RISK,
                ),
                handler=echo,
            )
        )
        engine = AgentExecutionEngine(
            store=execution_store,
            registry=registry,
            permission_manager=PermissionManager(
                PermissionPolicy(
                    auto_approve={
                        PermissionLevel.READ_ONLY,
                        PermissionLevel.LOW_RISK,
                    }
                )
            ),
            planner=_OneStepPlanner(),
            task_engine=TaskEngine(task_store),
        )
        try:
            record = await engine.run("benchmark agent", session_id="bench-agent")
            return {
                "status": record.status.value,
                "steps": len(record.steps),
                "tokens": record.input_tokens + record.output_tokens,
            }
        finally:
            execution_store.close()
            task_store.close()


async def tool_registry_case() -> dict[str, object]:
    registry = ToolRegistry()

    async def echo(value: str) -> str:
        return value

    registry.register(
        RuntimeTool(
            spec=ToolSpec(
                name="benchmark_tool",
                description="Benchmark tool.",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                permission=PermissionLevel.LOW_RISK,
            ),
            handler=echo,
        )
    )
    result = await registry.call("benchmark_tool", {"value": "ok"})
    invalid_rejected = False
    try:
        await registry.call("benchmark_tool", {"value": "ok", "extra": True})
    except ValueError:
        invalid_rejected = True
    return {"result": result, "invalid_rejected": invalid_rejected}
