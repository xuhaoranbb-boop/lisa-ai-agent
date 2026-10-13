from pathlib import Path

import pytest

from jarvis.runtime.browser import BrowserObservation
from jarvis.runtime.contracts import PermissionLevel, ToolSpec
from jarvis.runtime.execution import (
    AgentExecutionEngine,
    ExecutionStatus,
    ExecutionStore,
)
from jarvis.runtime.mcp import MCPManager, MCPTool, MCPToolAdapter
from jarvis.runtime.permissions import (
    ApprovalDecision,
    ConfirmationManager,
    PermissionManager,
    PermissionPolicy,
)
from jarvis.runtime.tasks import TaskEngine, TaskStep, TaskStore
from jarvis.runtime.tooling import RuntimeTool, ToolRegistry


class FakePlanner:
    async def create_plan(self, goal: str, context: str = "") -> list[TaskStep]:
        return [
            TaskStep(
                id="step-1",
                title="Write value",
                tool="write_value",
                metadata={"arguments": {"value": "ok"}},
            )
        ]


class ReplanningFailure:
    async def create_plan(self, goal: str, context: str = "") -> list[TaskStep]:
        return [
            TaskStep(
                id="step-fail",
                title="Always fails",
                tool="always_fail",
                metadata={"arguments": {}},
            )
        ]


@pytest.mark.asyncio
async def test_execution_engine_runs_plan_and_persists_steps(tmp_path: Path) -> None:
    task_store = TaskStore(tmp_path / "jarvis.db")
    execution_store = ExecutionStore(tmp_path / "jarvis.db")
    registry = ToolRegistry()

    async def write_value(value: str) -> dict[str, object]:
        return {"ok": True, "value": value}

    registry.register(
        RuntimeTool(
            spec=ToolSpec(
                name="write_value",
                description="Write a value.",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                permission=PermissionLevel.LOW_RISK,
                category="SYSTEM",
            ),
            handler=write_value,
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
                    PermissionLevel.MEDIUM_RISK,
                }
            )
        ),
        planner=FakePlanner(),
        task_engine=TaskEngine(task_store),
    )
    try:
        record = await engine.run("Test execution", session_id="session-1")
        assert record.status == ExecutionStatus.COMPLETED
        assert record.steps[0].status == "COMPLETED"
        assert "ok" in record.steps[0].result
        loaded = execution_store.get(record.id)
        assert loaded is not None
        assert loaded.task_id == record.task_id
    finally:
        execution_store.close()
        task_store.close()


@pytest.mark.asyncio
async def test_execution_engine_retry_and_replan_limit(tmp_path: Path) -> None:
    task_store = TaskStore(tmp_path / "jarvis.db")
    execution_store = ExecutionStore(tmp_path / "jarvis.db")
    registry = ToolRegistry()

    async def always_fail() -> dict[str, object]:
        raise TimeoutError("temporary network timeout")

    registry.register(
        RuntimeTool(
            spec=ToolSpec(
                name="always_fail",
                description="Always fails.",
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                permission=PermissionLevel.LOW_RISK,
            ),
            handler=always_fail,
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
                    PermissionLevel.MEDIUM_RISK,
                }
            )
        ),
        planner=ReplanningFailure(),
        task_engine=TaskEngine(task_store),
        max_retries=2,
    )
    try:
        record = await engine.run("Fail safely", session_id="session-1")
        assert record.status == ExecutionStatus.FAILED
        assert all(step.retry_count <= 2 for step in record.steps)
    finally:
        execution_store.close()
        task_store.close()


@pytest.mark.asyncio
async def test_confirmation_manager_requires_two_critical_confirmations() -> None:
    prompts: list[int] = []

    async def confirm(_level, _action, _resource, metadata) -> str:  # type: ignore[no-untyped-def]
        prompts.append(int(metadata["confirmation_stage"]))
        return ApprovalDecision.ALLOW_ONCE

    manager = ConfirmationManager(confirm)
    result = await manager.request(
        PermissionLevel.CRITICAL,
        "transfer_funds",
        "account",
        {},
    )
    assert result == ApprovalDecision.ALLOW_ONCE
    assert prompts == [1, 2]


@pytest.mark.asyncio
async def test_tool_registry_validates_arguments() -> None:
    registry = ToolRegistry()

    async def echo(value: str) -> str:
        return value

    registry.register(
        RuntimeTool(
            spec=ToolSpec(
                name="echo",
                description="Echo.",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            ),
            handler=echo,
        )
    )
    assert await registry.call("echo", {"value": "ok"}) == "ok"
    with pytest.raises(ValueError, match="required"):
        await registry.call("echo", {})
    with pytest.raises(ValueError, match="does not accept"):
        await registry.call("echo", {"value": "ok", "extra": True})


@pytest.mark.asyncio
async def test_mcp_tool_adapter_registers_discovered_tools() -> None:
    class FakeClient:
        def __init__(self, _config) -> None:  # type: ignore[no-untyped-def]
            pass

        async def connect(self) -> None:
            pass

        async def list_tools(self) -> list[MCPTool]:
            return [
                MCPTool(
                    server="demo",
                    name="echo",
                    description="Echo.",
                    input_schema={
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                    },
                )
            ]

        async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
            return {"name": name, "arguments": arguments}

        async def close(self) -> None:
            pass

    manager = MCPManager(client_factory=FakeClient)
    from jarvis.runtime.mcp import MCPServerConfig

    await manager.connect(MCPServerConfig(name="demo", command=("demo",)))
    registry = ToolRegistry()
    names = MCPToolAdapter(manager).register_tools(registry)
    assert names == ["demo.echo"]
    assert registry.get("demo.echo") is not None
    await manager.close()


@pytest.mark.asyncio
async def test_browser_tool_uses_adapter_observation() -> None:
    from jarvis.runtime.browser import BrowserTool

    class FakeBrowser:
        async def open(self, url: str) -> BrowserObservation:
            return BrowserObservation(url=url, title="Example", text="hello", status=200)

        async def read(self, url: str) -> BrowserObservation:
            return await self.open(url)

        async def click(self, selector: str) -> BrowserObservation:
            return BrowserObservation(url="https://example.com", title=selector, text="clicked")

        async def type_text(self, selector: str, text: str) -> BrowserObservation:
            return BrowserObservation(url="https://example.com", title=selector, text=text)

        async def scroll(self, delta: int) -> BrowserObservation:
            return BrowserObservation(url="https://example.com", title="scroll", text=str(delta))

        async def extract(self, selector: str = "body") -> BrowserObservation:
            return BrowserObservation(url="https://example.com", title=selector, text="body")

        async def close(self) -> None:
            pass

    tool = BrowserTool(FakeBrowser())
    result = await tool.open("https://example.com")
    assert result["title"] == "Example"
    assert result["status"] == 200
