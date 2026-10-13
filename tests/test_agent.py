from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis.agent import Agent, _select_model_role, build_system_prompt, tool_hint
from jarvis.config import Settings
from jarvis.memory import MemoryStore
from jarvis.model import (
    ModelReply,
    ModelStreamChunk,
    StreamToolCallDelta,
    ToolCall,
)
from jarvis.persona import PersonaManager
from jarvis.runtime import (
    ExecutionStatus,
    ModelRole,
    TaskEngine,
    TaskStatus,
    TaskStep,
    TaskStore,
)


class FakeModel:
    def __init__(self) -> None:
        self.calls = 0
        self.messages: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        self.messages.append(messages)
        self.calls += 1
        if self.calls == 1:
            return ModelReply(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="remember",
                        arguments={"content": "The user likes tea.", "category": "preference"},
                    )
                ],
                raw_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "remember",
                                "arguments": (
                                    '{"content": "The user likes tea.", '
                                    '"category": "preference"}'
                                ),
                            },
                        }
                    ],
                },
            )
        return ModelReply(
            content="记下了。",
            tool_calls=[],
            raw_message={"role": "assistant", "content": "记下了。"},
        )


class StreamingModel(FakeModel):
    async def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        yield "你好，"
        yield "我是 Jarvis。"


class StreamingToolModel:
    def __init__(self) -> None:
        self.turns = 0
        self.messages: list[list[dict[str, Any]]] = []

    async def stream_events(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ):
        self.messages.append(messages)
        self.turns += 1
        if self.turns == 1:
            yield ModelStreamChunk(
                tool_calls=(
                    StreamToolCallDelta(
                        index=0,
                        id="call-stream-1",
                        name="remember",
                        arguments='{"content":"The user likes tea.",',
                    ),
                    StreamToolCallDelta(
                        index=0,
                        arguments='"category":"preference"}',
                    ),
                ),
                finish_reason="tool_calls",
            )
            return
        yield ModelStreamChunk(content="记")
        yield ModelStreamChunk(content="下了。")

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        raise AssertionError("Streaming tool path must not call complete().")


@pytest.mark.asyncio
async def test_agent_executes_tools_and_persists_messages(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path)
    memory = MemoryStore(settings.database_path)
    model = FakeModel()
    agent = Agent(settings, memory, model)
    try:
        reply = await agent.ask("Remember that I like tea.", session_id="test")
        assert reply.text == "记下了。"
        assert reply.tool_calls == 1
        assert memory.recall("tea")[0].content == "The user likes tea."
        assert [message.role for message in memory.history("test", 10)] == [
            "user",
            "assistant",
        ]
    finally:
        memory.close()


def test_tool_hint_routes_common_chinese_requests() -> None:
    assert "get_current_time" in (tool_hint("现在上海几点？") or "")
    assert "get_weather" in (tool_hint("查一下上海天气") or "")
    assert "remember" in (tool_hint("请记住我的偏好") or "")


def test_model_role_selection() -> None:
    assert _select_model_role("帮我重构这个 Python 项目") == ModelRole.CODE
    assert _select_model_role("分析一下系统架构方案") == ModelRole.REASONING
    assert _select_model_role("看看这张截图") == ModelRole.VISION
    assert _select_model_role("你好") == ModelRole.CHAT


def test_system_prompt_includes_current_task_state(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path)
    prompt = build_system_prompt(
        settings,
        [],
        "Goal: inspect project\n1. [EXECUTING] Run tests tool=run_workspace_command",
    )
    assert "Current task context:" in prompt
    assert "Run tests" in prompt


@pytest.mark.asyncio
async def test_agent_injects_persona_profile_into_model_prompt(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path)
    memory = MemoryStore(settings.database_path)
    persona = PersonaManager(settings)
    model = FakeModel()
    agent = Agent(settings, memory, model, persona=persona)
    try:
        await agent.ask("切换到专注模式", session_id="persona-test")
        prompt = str(model.messages[0][0]["content"])
        assert "Persona profile:" in prompt
        assert "Current mode: focused." in prompt
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_agent_persists_task_state(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path)
    memory = MemoryStore(settings.database_path)
    task_store = TaskStore(settings.database_path)
    task_engine = TaskEngine(task_store)
    model = FakeModel()
    agent = Agent(settings, memory, model, task_engine=task_engine)
    try:
        reply = await agent.ask("Remember that I like tea.", session_id="task-test")
        task = task_engine.get_task(reply.task_id or "")
        assert task is not None
        assert task.status == TaskStatus.COMPLETED
        assert "Current task context:" in model.messages[0][0]["content"]
        assert "Remember that I like tea." in model.messages[0][0]["content"]
        assert any(
            "Task completed" in item.content
            for item in memory.list_memories()
        )
    finally:
        memory.close()
        task_store.close()


@pytest.mark.asyncio
async def test_agent_delegates_planned_tasks_to_execution_engine(
    tmp_path: Path,
) -> None:
    class FakeExecutionEngine:
        def __init__(self) -> None:
            self.calls = 0

        async def run(
            self,
            goal: str,
            *,
            session_id: str,
            task_id: str,
            cancellation: object,
        ) -> object:
            self.calls += 1
            return SimpleNamespace(
                status=ExecutionStatus.COMPLETED,
                error="",
                result="",
                task_id=task_id,
                steps=[
                    SimpleNamespace(
                        result='{"ok": true, "value": "checked"}',
                    )
                ],
            )

    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        workspace=tmp_path,
    )
    memory = MemoryStore(settings.database_path)
    execution_engine = FakeExecutionEngine()
    agent = Agent(
        settings,
        memory,
        FakeModel(),
        execution_engine=execution_engine,  # type: ignore[arg-type]
    )
    try:
        reply = await agent.ask("检查项目")
        assert execution_engine.calls == 1
        assert "任务完成" in reply.text
        assert reply.tool_calls == 1
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_agent_streams_simple_chat_and_persists_reply(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path)
    memory = MemoryStore(settings.database_path)
    task_store = TaskStore(settings.database_path)
    task_engine = TaskEngine(task_store)
    agent = Agent(settings, memory, StreamingModel(), task_engine=task_engine)
    try:
        chunks = [
            chunk
            async for chunk in agent.ask_stream("你好", session_id="stream-test")
        ]
        assert "".join(chunks) == "你好，我是 Jarvis。"
        history = memory.history("stream-test", 10)
        assert [message.role for message in history] == ["user", "assistant"]
    finally:
        memory.close()
        task_store.close()


@pytest.mark.asyncio
async def test_agent_streams_tool_calls_and_final_answer(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path)
    memory = MemoryStore(settings.database_path)
    model = StreamingToolModel()
    agent = Agent(settings, memory, model)
    try:
        chunks = [
            chunk
            async for chunk in agent.ask_stream("请记住我喜欢茶。", session_id="tool-stream")
        ]

        assert "".join(chunks) == "记下了。"
        assert model.turns == 2
        assert memory.recall("tea")[0].content == "The user likes tea."
        assert any(
            message.get("role") == "tool"
            for message in model.messages[1]
        )
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_agent_continues_a_persisted_task(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path)
    memory = MemoryStore(settings.database_path)
    task_store = TaskStore(settings.database_path)
    task_engine = TaskEngine(task_store)
    agent = Agent(settings, memory, FakeModel(), task_engine=task_engine)
    try:
        task = await task_engine.create_task(
            "Remember the user's tea preference.",
            session_id="continue-test",
        )
        await task_engine.set_plan(
            task.id,
            [
                TaskStep(
                    id="step-1",
                    title="Persist the preference",
                    tool="remember",
                )
            ],
        )

        reply = await agent.continue_task(task.id)
        resumed = task_engine.get_task(task.id)

        assert reply.text == "记下了。"
        assert resumed is not None
        assert resumed.status == TaskStatus.COMPLETED
        assert resumed.plan[0].status == TaskStatus.COMPLETED
    finally:
        memory.close()
        task_store.close()
