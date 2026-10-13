import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from jarvis.config import Settings
from jarvis.memory import MemoryStore
from jarvis.model import ModelReply
from jarvis.runtime import (
    CancellationToken,
    EventBus,
    MCPTool,
    OperationCancelled,
    ReminderService,
    ReminderStore,
    TaskScheduler,
)
from jarvis.tools import Tool, Toolbox, _extract_readable_html


@pytest.mark.asyncio
async def test_workspace_path_cannot_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(workspace=workspace, data_dir=tmp_path / "data")
    memory = MemoryStore(settings.database_path)
    toolbox = Toolbox(settings, memory)
    try:
        with pytest.raises(PermissionError):
            await toolbox.read_workspace_file("../secret.txt")
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_workspace_read_is_bounded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("hello", encoding="utf-8")
    settings = Settings(workspace=workspace, data_dir=tmp_path / "data")
    memory = MemoryStore(settings.database_path)
    toolbox = Toolbox(settings, memory)
    try:
        result = await toolbox.read_workspace_file("note.txt")
        assert result == {"path": "note.txt", "content": "hello"}
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_current_time_accepts_city_names(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path)
    memory = MemoryStore(settings.database_path)
    toolbox = Toolbox(settings, memory)
    try:
        result = await toolbox.get_current_time("Shanghai")
        assert result["timezone"] == "Asia/Shanghai"
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_memory_tools_support_update_and_delete(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(workspace=workspace, data_dir=tmp_path / "data")
    memory = MemoryStore(settings.database_path)
    toolbox = Toolbox(settings, memory)
    try:
        stored = await toolbox.remember("old preference", "preference", importance=2)
        updated = await toolbox.update_memory(
            int(stored["id"]),
            content="new preference",
            importance=5,
        )
        assert updated["ok"] is True
        assert updated["content"] == "new preference"
        assert updated["importance"] == 5

        deleted = await toolbox.forget_memory(int(stored["id"]))
        assert deleted == {"ok": True, "id": stored["id"]}
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_mcp_tools_register_into_toolbox(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(workspace=workspace, data_dir=tmp_path / "data")
    memory = MemoryStore(settings.database_path)
    toolbox = Toolbox(settings, memory)

    class FakeMCPManager:
        def tools(self) -> list[MCPTool]:
            return [
                MCPTool(
                    server="demo",
                    name="echo",
                    description="Echo a value.",
                    input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
                )
            ]

        async def call_tool(self, server: str, name: str, arguments: dict[str, object]) -> object:
            return {"server": server, "name": name, "arguments": arguments}

    try:
        toolbox.register_mcp_tools(FakeMCPManager())  # type: ignore[arg-type]
        result = await toolbox.call("mcp_demo_echo", {"value": "hello"})
        assert result == {
            "ok": True,
            "result": {
                "server": "demo",
                "name": "echo",
                "arguments": {"value": "hello"},
            },
        }
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_run_workspace_command_is_allowlisted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("value = 1\n", encoding="utf-8")
    settings = Settings(workspace=workspace, data_dir=tmp_path / "data")
    memory = MemoryStore(settings.database_path)
    toolbox = Toolbox(settings, memory)
    try:
        result = await toolbox.run_workspace_command(
            "python",
            ["-m", "compileall", "."],
            timeout=30,
        )
        assert result["returncode"] == 0
        with pytest.raises(PermissionError):
            await toolbox.run_workspace_command("git", ["reset", "--hard"])
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_schedule_reminder_registers_proactive_job(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(workspace=workspace, data_dir=tmp_path / "data")
    memory = MemoryStore(settings.database_path)
    bus = EventBus()
    scheduler = TaskScheduler(bus)
    reminder_store = ReminderStore(settings.database_path)
    reminder_service = ReminderService(reminder_store, scheduler, bus)
    toolbox = Toolbox(
        settings,
        memory,
        scheduler=scheduler,
        reminder_service=reminder_service,
        event_bus=bus,
    )
    try:
        result = await toolbox.schedule_reminder("喝水", 60)
        assert result["status"] == "SCHEDULED"
        assert reminder_service.list()[0].message == "喝水"
    finally:
        reminder_store.close()
        memory.close()


def test_web_page_text_extraction() -> None:
    title, text, links = _extract_readable_html(
        """
        <html>
          <head>
            <title>Jarvis Test</title>
            <style>ignored style</style>
          </head>
          <body>
            <h1>Hello</h1>
            <p>Readable text.</p>
            <a href="https://example.com/docs">Documentation</a>
          </body>
        </html>
        """
    )

    assert title == "Jarvis Test"
    assert "Hello" in text
    assert "Readable text." in text
    assert "ignored style" not in text
    assert links == [
        {"text": "Documentation", "url": "https://example.com/docs"}
    ]


@pytest.mark.asyncio
async def test_vision_tool_uses_configured_provider(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(workspace=workspace, data_dir=tmp_path / "data")
    memory = MemoryStore(settings.database_path)

    class FakeVisionProvider:
        async def vision(self, messages, image):  # type: ignore[no-untyped-def]
            assert image == b"png"
            return ModelReply(
                content="A test screen.",
                tool_calls=[],
                raw_message={"role": "assistant", "content": "A test screen."},
            )

    toolbox = Toolbox(
        settings,
        memory,
        vision_provider=FakeVisionProvider(),
    )
    toolbox.desktop.execute = AsyncMock(return_value=b"png")  # type: ignore[method-assign]
    try:
        result = await toolbox.computer_analyze_screen("What is on screen?")
        assert result == {
            "description": "A test screen.",
            "bytes": 3,
            "format": "png",
        }
        assert "computer_analyze_screen" in [
            schema["function"]["name"] for schema in toolbox.schemas()
        ]
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_background_command_emits_completion(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(workspace=workspace, data_dir=tmp_path / "data")
    memory = MemoryStore(settings.database_path)
    bus = EventBus()
    scheduler = TaskScheduler(bus)
    toolbox = Toolbox(
        settings,
        memory,
        scheduler=scheduler,
        event_bus=bus,
    )
    toolbox.run_workspace_command = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "command": "python",
            "args": ["-m", "compileall", "."],
            "cwd": ".",
            "returncode": 0,
            "timed_out": False,
            "stdout": "ok",
            "stderr": "",
        }
    )
    try:
        await toolbox.run_background_command(
            "python",
            ["-m", "compileall", "."],
        )
        await scheduler.start()
        for _ in range(30):
            if any(
                str(event.type) == "BACKGROUND_TASK_COMPLETED"
                for event in bus.history()
            ):
                break
            await asyncio.sleep(0.02)
        assert any(
            str(event.type) == "BACKGROUND_TASK_COMPLETED"
            for event in bus.history()
        )
    finally:
        await scheduler.stop()
        memory.close()


@pytest.mark.asyncio
async def test_tool_timeout_returns_structured_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(workspace=workspace, data_dir=tmp_path / "data")
    memory = MemoryStore(settings.database_path)
    toolbox = Toolbox(settings, memory)

    async def slow_tool() -> str:
        await asyncio.sleep(0.1)
        return "late"

    try:
        toolbox.register(
            Tool(
                name="slow_tool",
                description="Slow test tool.",
                parameters={"type": "object", "properties": {}},
                handler=slow_tool,
                timeout_seconds=0.01,
            )
        )
        result = await toolbox.call("slow_tool", {})
        assert result["ok"] is False
        assert "TimeoutError" in result["error"]
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_tool_call_stops_when_cancellation_token_is_cancelled(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(workspace=workspace, data_dir=tmp_path / "data")
    memory = MemoryStore(settings.database_path)
    toolbox = Toolbox(settings, memory)
    cancellation = CancellationToken()

    async def slow_tool() -> str:
        await asyncio.sleep(1)
        return "late"

    try:
        toolbox.register(
            Tool(
                name="slow_tool",
                description="Slow test tool.",
                parameters={"type": "object", "properties": {}},
                handler=slow_tool,
                timeout_seconds=2,
            )
        )
        call = asyncio.create_task(
            toolbox.call("slow_tool", {}, cancellation=cancellation)
        )
        await asyncio.sleep(0.05)
        cancellation.cancel("stop")

        with pytest.raises(OperationCancelled, match="stop"):
            await call
    finally:
        memory.close()
