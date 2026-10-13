import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.model import ModelReply, ModelStreamChunk
from jarvis.runtime.budget import TokenBudgetManager
from jarvis.runtime.contracts import PermissionLevel, ToolSpec
from jarvis.runtime.desktop import DesktopController
from jarvis.runtime.events import EventBus
from jarvis.runtime.execution import AgentExecutionEngine, ExecutionStore
from jarvis.runtime.file_intelligence import FileIntelligence
from jarvis.runtime.monitor import SystemMonitor
from jarvis.runtime.multimodal import ScreenObserver, VisionAgent
from jarvis.runtime.permissions import PermissionManager, PermissionPolicy
from jarvis.runtime.plugins import PluginManager
from jarvis.runtime.router import BudgetAwareModelProvider, FailoverModelProvider
from jarvis.runtime.tasks import TaskEngine, TaskStore
from jarvis.runtime.tooling import RuntimeTool, ToolRegistry
from jarvis.runtime.trace import AgentTrace, AgentTraceStore, TraceRecord
from jarvis.runtime.workflow import (
    WorkflowEngine,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStore,
)


class SimplePlanner:
    async def create_plan(self, goal: str, context: str = ""):  # type: ignore[no-untyped-def]
        return []


@pytest.mark.asyncio
async def test_file_intelligence_reads_and_summarizes(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("Important project decision\nNext action: test", encoding="utf-8")
    intelligence = FileIntelligence()

    insight = await intelligence.inspect(path)

    assert insight.category == "text"
    assert "Important project decision" in insight.text
    assert insight.summary


def test_token_budget_tracks_request_task_and_daily_usage() -> None:
    manager = TokenBudgetManager(
        request_token_budget=1_000,
        task_token_budget=2_000,
        daily_token_budget=5_000,
        input_cost_per_million=1.0,
    )
    usage = manager.record(
        input_text="hello world",
        output_text="response",
        task_id="task-1",
        request_id="request-1",
    )
    assert usage.total_tokens > 0
    assert manager.task_usage("task-1").total_tokens == usage.total_tokens
    assert manager.daily_usage().total_tokens == usage.total_tokens
    assert manager.can_spend(100, task_id="task-1", request_id="request-2")


@pytest.mark.asyncio
async def test_model_router_failover_preserves_request_context() -> None:
    class FailedProvider:
        async def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            raise RuntimeError("rate limited")

        async def stream_events(self, messages, tools):  # type: ignore[no-untyped-def]
            raise RuntimeError("rate limited")
            yield ModelStreamChunk()

    class HealthyProvider:
        async def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            assert messages[-1]["content"] == "context"
            return ModelReply(
                content="ok",
                tool_calls=[],
                raw_message={"role": "assistant", "content": "ok"},
            )

        async def stream_events(self, messages, tools):  # type: ignore[no-untyped-def]
            yield ModelStreamChunk(content="ok")

    provider = FailoverModelProvider(  # type: ignore[arg-type]
        [FailedProvider(), HealthyProvider()]  # type: ignore[list-item]
    )
    reply = await provider.complete([{"role": "user", "content": "context"}], [])
    assert reply.content == "ok"
    chunks = [chunk async for chunk in provider.stream_events(
        [{"role": "user", "content": "context"}],
        [],
    )]
    assert "".join(chunk.content for chunk in chunks) == "ok"


@pytest.mark.asyncio
async def test_screen_observer_uses_cache_for_unchanged_screen() -> None:
    calls = 0

    class Adapter:
        async def window_list(self):  # type: ignore[no-untyped-def]
            return [{"ProcessName": "Code", "MainWindowTitle": "project"}]

        async def screenshot(self) -> bytes:
            return b"same-screen"

        async def ui_tree(self, max_items: int = 180):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            return [
                {
                    "name": "Run",
                    "controlType": "Button",
                    "automationId": "run",
                    "bounds": {"x": 1, "y": 2, "width": 10, "height": 10},
                    "enabled": True,
                }
            ]

    controller = SimpleNamespace(
        adapter=Adapter(),
        observe=lambda include_screenshot=False: _observe(),
    )

    async def _observe():  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            windows=await controller.adapter.window_list(),
            screenshot=await controller.adapter.screenshot(),
        )

    observer = ScreenObserver(controller, cache_seconds=60)  # type: ignore[arg-type]
    first = await observer.observe()
    second = await observer.observe()

    assert first.screenshot_hash == second.screenshot_hash
    assert calls == 1
    assert first.buttons == ["Run"]


@pytest.mark.asyncio
async def test_desktop_observe_degrades_when_screenshot_fails() -> None:
    class Adapter:
        async def window_list(self):  # type: ignore[no-untyped-def]
            return [{"ProcessName": "Code"}]

        async def screenshot(self) -> bytes:
            raise FileNotFoundError("screenshot missing")

    observation = await DesktopController(Adapter()).observe(include_screenshot=True)  # type: ignore[arg-type]
    assert observation.windows == [{"ProcessName": "Code"}]
    assert observation.screenshot is None


@pytest.mark.asyncio
async def test_workflow_runs_dependencies_and_persists(tmp_path: Path) -> None:
    task_store = TaskStore(tmp_path / "jarvis.db")
    execution_store = ExecutionStore(tmp_path / "jarvis.db")
    workflow_store = WorkflowStore(tmp_path / "jarvis.db")
    registry = ToolRegistry()

    async def record(value: str) -> dict[str, str]:
        return {"value": value}

    registry.register(
        RuntimeTool(
            spec=ToolSpec(
                name="record",
                description="Record a value.",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
                permission=PermissionLevel.LOW_RISK,
            ),
            handler=record,
        )
    )
    execution_engine = AgentExecutionEngine(
        store=execution_store,
        registry=registry,
        permission_manager=PermissionManager(
            PermissionPolicy(
                auto_approve={
                    PermissionLevel.READ_ONLY,
                    PermissionLevel.LOW_RISK,
                    PermissionLevel.MEDIUM_RISK,
                }
            )
        ),
        planner=SimplePlanner(),
        task_engine=TaskEngine(task_store),
    )
    workflow_engine = WorkflowEngine(
        store=workflow_store,
        execution_engine=execution_engine,
    )
    try:
        workflow = await workflow_engine.create(
            name="two-step",
            session_id="session-1",
            steps=[
                WorkflowStep(
                    id="one",
                    title="First",
                    goal="First",
                    tool="record",
                    arguments={"value": "one"},
                ),
                WorkflowStep(
                    id="two",
                    title="Second",
                    goal="Second",
                    tool="record",
                    arguments={"value": "two"},
                    dependencies=["one"],
                ),
            ],
        )
        result = await workflow_engine.run(workflow.id)
        assert result.status == WorkflowStatus.COMPLETED
        assert [run.status for run in result.step_runs.values()] == [
            "COMPLETED",
            "COMPLETED",
        ]
    finally:
        workflow_store.close()
        execution_store.close()
        task_store.close()


def test_plugin_manager_loads_and_registers_enabled_plugind(tmp_path: Path) -> None:
    manifest = tmp_path / "plugin.json"
    manifest.write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "1.0",
                "enabled": True,
                "tools": [
                    {
                        "name": "demo.echo",
                        "description": "Echo",
                        "handler": "jarvis.runtime.budget:estimate_tokens",
                        "permission": "LOW_RISK",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manager = PluginManager()
    manager.load_file(manifest)
    registry = ToolRegistry()

    assert manager.register_tools(registry) == ["demo.echo"]
    assert registry.get("demo.echo") is not None


@pytest.mark.asyncio
async def test_system_monitor_and_trace_record_events(tmp_path: Path) -> None:
    class Collector:
        async def get_system_info(self):  # type: ignore[no-untyped-def]
            return {
                "cpu_count": 8,
                "memory_percent": 95,
                "disk_free_gb": 2,
            }

    bus = EventBus()
    monitor = SystemMonitor(Collector(), bus)
    alerts = await monitor.check()
    assert {alert["type"] for alert in alerts} == {"memory", "disk"}

    store = AgentTraceStore(tmp_path / "trace.db")
    trace = AgentTrace(store)
    try:
        trace.store.record(
            TraceRecord(
                id=0,
                task_id="task-1",
                execution_id="exec-1",
                turn_id="turn-1",
                event_type="TOOL_RESULT",
                model="deepseek-chat",
                tool="get_current_time",
                latency_ms=12,
                input_tokens=4,
                output_tokens=3,
                estimated_cost=0.0,
                error="",
                data={"ok": True},
            )
        )
        records = store.list(task_id="task-1")
        assert records[0].tool == "get_current_time"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_vision_agent_verifies_desktop_action() -> None:
    class FakeObserver:
        provider = None

        async def observe(self) -> SimpleNamespace:
            return SimpleNamespace(
                text="VS Code window is visible.",
                to_dict=lambda: {"text": "VS Code window is visible."},
            )

    class FakeVisionProvider:
        async def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            return ModelReply(
                content='{"verified": true, "reason": "VS Code is visible."}',
                tool_calls=[],
                raw_message={},
            )

    agent = VisionAgent(  # type: ignore[arg-type]
        FakeObserver(),
        FakeVisionProvider(),
    )
    result = await agent.verify_action("open VS Code")

    assert result.data["verified"] is True
    assert "visible" in result.text


@pytest.mark.asyncio
async def test_budget_aware_provider_downshifts_and_recovers() -> None:
    class FakeProvider:
        def __init__(self, content: str) -> None:
            self.content = content
            self.calls = 0

        async def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            self.calls += 1
            return ModelReply(
                content=self.content,
                tool_calls=[],
                raw_message={},
            )

    class FailingProvider(FakeProvider):
        async def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise RuntimeError("low-cost model unavailable")

    budget = TokenBudgetManager(
        daily_token_budget=100,
        downshift_ratio=0.8,
    )
    budget.record(input_text="你" * 100)
    primary = FakeProvider("primary")
    low_cost = FakeProvider("low-cost")
    provider = BudgetAwareModelProvider(
        primary,  # type: ignore[arg-type]
        low_cost,  # type: ignore[arg-type]
        budget,
        threshold=0.8,
    )

    reply = await provider.complete([], [])
    assert reply.content == "low-cost"
    assert low_cost.calls == 1
    assert primary.calls == 0

    failing = FailingProvider("unused")
    fallback_provider = BudgetAwareModelProvider(
        primary,  # type: ignore[arg-type]
        failing,  # type: ignore[arg-type]
        budget,
        threshold=0.8,
    )
    fallback_reply = await fallback_provider.complete([], [])
    assert fallback_reply.content == "primary"
    assert failing.calls == 1
    assert primary.calls == 1
