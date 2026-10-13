import asyncio
import sys
from pathlib import Path

import pytest

from jarvis.config import Settings
from jarvis.memory import MemoryStore
from jarvis.model import ModelReply
from jarvis.runtime import (
    ApprovalBroker,
    ApprovalDecision,
    CancellationToken,
    ComputerAgentLoop,
    DesktopController,
    DesktopObservation,
    EventBus,
    EventType,
    LLMPlanner,
    MCPManager,
    MCPServerConfig,
    MCPTool,
    ModelRole,
    ModelRouter,
    OperationCancelled,
    PermissionDenied,
    PermissionLevel,
    PermissionManager,
    PermissionPolicy,
    ReminderStatus,
    ReminderStore,
    RuntimeEvent,
    StdioMCPClient,
    TaskEngine,
    TaskScheduler,
    TaskStatus,
    TaskStep,
    TaskStore,
    TimingRecorder,
    ToolRegistry,
    mcp_server_configs_from_settings,
    permission_policy_from_settings,
)
from jarvis.tools import Tool, Toolbox


def test_cancellation_propagates_to_children() -> None:
    parent = CancellationToken()
    child = parent.child()
    grandchild = child.child()

    parent.cancel("stop")

    assert parent.cancelled
    assert child.cancelled
    assert grandchild.cancelled
    with pytest.raises(OperationCancelled, match="stop"):
        grandchild.raise_if_cancelled()


@pytest.mark.asyncio
async def test_event_bus_filters_and_replays_history() -> None:
    bus = EventBus(history_size=4)
    seen: list[str] = []
    unsubscribe = bus.subscribe(
        lambda event: seen.append(str(event.type)),
        {EventType.TASK_START},
    )

    await bus.emit_async(RuntimeEvent(EventType.TASK_START, {"task": "one"}))
    await bus.emit_async(RuntimeEvent(EventType.TOOL_START, {"tool": "time"}))
    unsubscribe()
    await bus.emit_async(RuntimeEvent(EventType.TASK_COMPLETE, {"task": "one"}))

    assert seen == ["TASK_START"]
    assert [str(event.type) for event in bus.history()] == [
        "TASK_START",
        "TOOL_START",
        "TASK_COMPLETE",
    ]


def test_model_router_selects_role_models(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace=tmp_path,
        model="chat-model",
        voice_model="voice-model",
        reasoning_model="reason-model",
        code_model="code-model",
        vision_model="vision-model",
        vision_base_url="https://vision.example/v1",
        vision_api_key="vision-key",
        computer_model="computer-model",
        model_fallbacks="local-model",
        model_fallback_base_url="http://127.0.0.1:8001/v1",
        model_fallback_api_key="local-key",
    )
    router = ModelRouter(settings)

    assert router.route(ModelRole.CHAT).model == "chat-model"
    assert router.route(ModelRole.VOICE).model == "voice-model"
    assert router.route(ModelRole.REASONING).model == "reason-model"
    assert router.route(ModelRole.CODE).model == "code-model"
    assert router.route(ModelRole.VISION).model == "vision-model"
    assert router.route(ModelRole.VISION).base_url == "https://vision.example/v1"
    assert router.route(ModelRole.VISION).api_key == "vision-key"
    vision_provider = router.provider_for(ModelRole.VISION)
    assert (
        vision_provider.settings.model_api_key.get_secret_value()
        == "vision-key"
    )
    assert router.route(ModelRole.COMPUTER).model == "computer-model"
    fallback_routes = router.routes_for(ModelRole.CHAT)
    assert len(fallback_routes) == 2
    assert fallback_routes[1].model == "local-model"
    assert fallback_routes[1].base_url == "http://127.0.0.1:8001/v1"
    assert fallback_routes[1].api_key == "local-key"
    assert fallback_routes[1].local is True


def test_timing_recorder_records_sample_and_event() -> None:
    bus = EventBus()
    recorder = TimingRecorder(bus)
    started_at = recorder.start()

    sample = recorder.mark("LLM_START", started_at, task_id="task-1")

    assert sample.elapsed_ms >= 0
    assert bus.history()[-1].task_id == "task-1"
    assert bus.history()[-1].type == EventType.LLM_START


@pytest.mark.asyncio
async def test_permission_manager_auto_allows_and_blocks() -> None:
    manager = PermissionManager(
        PermissionPolicy(
            blocked_commands=("format *",),
            allowed_apps=("Code.exe", "chrome.exe"),
        )
    )

    read = await manager.decide(PermissionLevel.READ_ONLY, "read_file", "notes.md")
    assert read.allowed

    blocked = await manager.decide(PermissionLevel.NORMAL, "format *", "C:\\")
    assert not blocked.allowed

    outside = await manager.decide(PermissionLevel.LOW_RISK, "open_app", "notepad.exe")
    assert not outside.allowed

    with pytest.raises(PermissionDenied):
        await manager.require(PermissionLevel.NORMAL, "format *", "C:\\")


@pytest.mark.asyncio
async def test_high_risk_requires_confirmation() -> None:
    calls: list[str] = []

    async def confirm(_level, _action, _resource, _metadata) -> bool:
        calls.append(_action)
        return True

    manager = PermissionManager(confirmation_handler=confirm)
    decision = await manager.decide(PermissionLevel.HIGH_RISK, "delete_file", "old.txt")

    assert decision.allowed
    assert calls == ["delete_file"]


@pytest.mark.asyncio
async def test_approval_broker_resolves_high_risk_request() -> None:
    bus = EventBus()
    broker = ApprovalBroker(bus, timeout_seconds=2)
    manager = PermissionManager(confirmation_handler=broker.request)

    pending_task = asyncio.create_task(
        manager.decide(PermissionLevel.HIGH_RISK, "delete_file", "old.txt")
    )
    for _ in range(20):
        pending = await broker.pending()
        if pending:
            break
        await asyncio.sleep(0.01)
    assert pending
    assert pending[0].action == "delete_file"

    assert await broker.resolve(pending[0].id, ApprovalDecision.ALLOW_TASK)
    decision = await pending_task
    assert decision.allowed

    second = await manager.decide(PermissionLevel.HIGH_RISK, "delete_file", "old.txt")
    assert second.allowed
    assert [str(event.type) for event in bus.history()][:3] == [
        "PERMISSION_REQUEST",
        "PERMISSION_GRANTED",
        "PERMISSION_RESOLVED",
    ]


@pytest.mark.asyncio
async def test_task_engine_persists_and_resumes(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    bus = EventBus()
    engine = TaskEngine(store, bus)
    try:
        task = await engine.create_task("Check the project", session_id="session-1")
        planned = await engine.set_plan(
            task.id,
            [
                TaskStep(id="step-1", title="Open project", tool="open_app"),
                TaskStep(id="step-2", title="Run tests", tool="run_command"),
            ],
        )
        assert planned.status == TaskStatus.PLANNING

        executing = await engine.start_step(task.id, "step-1")
        assert executing.plan[0].status == TaskStatus.EXECUTING

        completed = await engine.complete_step(task.id, "step-1", "opened")
        assert completed.progress == 0.5

        paused = await engine.pause_task(task.id)
        resumed = await engine.resume_task(task.id)
        assert paused.status == TaskStatus.PAUSED
        assert resumed.status == TaskStatus.EXECUTING

        loaded = TaskStore(tmp_path / "tasks.db")
        try:
            assert loaded.get(task.id) is not None
        finally:
            loaded.close()
        assert [str(event.type) for event in bus.history()][:2] == [
            "TASK_START",
            "PLAN_CREATED",
        ]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_mcp_manager_discovers_and_calls_tools() -> None:
    class FakeMCPClient:
        def __init__(self, config: MCPServerConfig) -> None:
            self.config = config

        async def connect(self) -> None:
            pass

        async def list_tools(self) -> list[MCPTool]:
            return [
                MCPTool(
                    server=self.config.name,
                    name="echo",
                    description="Echo a value.",
                    input_schema={"type": "object"},
                )
            ]

        async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
            return {"name": name, "arguments": arguments}

        async def close(self) -> None:
            pass

    bus = EventBus()
    manager = MCPManager(bus, client_factory=FakeMCPClient)
    config = MCPServerConfig(name="demo", command=("demo",))
    tools = await manager.connect(config)

    assert [tool.qualified_name for tool in tools] == ["demo.echo"]
    result = await manager.call_tool("demo", "echo", {"value": 1})
    assert result == {"name": "echo", "arguments": {"value": 1}}
    assert [str(event.type) for event in bus.history()] == [
        "MCP_CONNECTED",
        "MCP_TOOL_CALL",
        "MCP_TOOL_RESULT",
    ]
    await manager.close()


@pytest.mark.asyncio
async def test_mcp_manager_reconnects_after_call_failure() -> None:
    class FlakyMCPClient:
        instances = 0
        calls = 0

        def __init__(self, config: MCPServerConfig) -> None:
            self.config = config
            self.closed = False
            FlakyMCPClient.instances += 1

        async def connect(self) -> None:
            pass

        async def list_tools(self) -> list[MCPTool]:
            return [
                MCPTool(
                    server=self.config.name,
                    name="echo",
                    description="Echo.",
                    input_schema={"type": "object"},
                )
            ]

        async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
            FlakyMCPClient.calls += 1
            if FlakyMCPClient.calls == 1:
                raise RuntimeError("connection dropped")
            return {"name": name, "arguments": arguments}

        async def close(self) -> None:
            self.closed = True

    bus = EventBus()
    manager = MCPManager(bus, client_factory=FlakyMCPClient)
    await manager.connect(MCPServerConfig(name="flaky", command=("demo",)))
    result = await manager.call_tool("flaky", "echo", {"value": 1})

    assert FlakyMCPClient.instances == 2
    assert result == {"name": "echo", "arguments": {"value": 1}}
    event_types = [str(event.type) for event in bus.history()]
    assert "MCP_RECONNECTED" in event_types
    assert event_types[-1] == "MCP_TOOL_RESULT"
    await manager.close()


@pytest.mark.asyncio
async def test_stdio_mcp_client_round_trip() -> None:
    server_code = """
import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18"}
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo",
                    "inputSchema": {"type": "object"},
                }
            ]
        }
    elif method == "tools/call":
        result = {"echo": message.get("params", {}).get("arguments", {})}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
"""
    client = StdioMCPClient(
        MCPServerConfig(
            name="stdio-test",
            command=(sys.executable, "-u", "-c", server_code),
            timeout_seconds=5,
        )
    )
    await client.connect()
    try:
        tools = await client.list_tools()
        assert [tool.qualified_name for tool in tools] == ["stdio-test.echo"]
        result = await client.call_tool("echo", {"value": "hello"})
        assert result == {"echo": {"value": "hello"}}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_tool_registry_enforces_permission_before_execution() -> None:
    registry = ToolRegistry()
    calls: list[str] = []

    class FakeToolbox:
        def schemas(self) -> list[dict[str, object]]:
            return [
                {
                    "type": "function",
                    "function": {
                        "name": "delete_file",
                        "description": "Delete one file.",
                        "parameters": {"type": "object"},
                    },
                }
            ]

        async def delete_file(self, path: str) -> str:
            calls.append(path)
            return "deleted"

    registry.register_toolbox(FakeToolbox())
    manager = PermissionManager(
        PermissionPolicy(
            auto_approve={PermissionLevel.READ_ONLY},
        )
    )

    with pytest.raises(PermissionDenied):
        await registry.call(
            "delete_file",
            {"path": "old.txt"},
            permission_manager=manager,
        )
    assert calls == []


@pytest.mark.asyncio
async def test_desktop_controller_and_computer_loop() -> None:
    class FakeDesktop:
        def __init__(self) -> None:
            self.opened: list[str] = []

        async def screenshot(self) -> bytes:
            return b"png"

        async def window_list(self) -> list[dict[str, object]]:
            return [{"title": "Code"}]

        async def window_focus(self, target: str | int) -> None:
            self.opened.append(str(target))

        async def app_open(self, application: str) -> None:
            self.opened.append(application)

        async def app_close(self, application: str) -> None:
            self.opened.append(f"close:{application}")

        async def mouse_move(self, x: int, y: int) -> None:
            pass

        async def mouse_click(self, x: int | None = None, y: int | None = None) -> None:
            pass

        async def mouse_double_click(self, x: int | None = None, y: int | None = None) -> None:
            pass

        async def keyboard_type(self, text: str) -> None:
            pass

        async def keyboard_press(self, key: str) -> None:
            pass

        async def scroll(self, delta: int) -> None:
            pass

    fake = FakeDesktop()
    controller = DesktopController(fake)
    await controller.execute("app_open", {"application": "Code.exe"})
    assert fake.opened == ["Code.exe"]

    calls: list[str] = []

    async def planner(
        goal: str,
        observation: DesktopObservation,
        history: list[dict[str, object]],
    ) -> dict[str, object]:
        calls.append(goal)
        if history:
            return {}
        return {"action": "app_open", "arguments": {"application": "Code.exe"}}

    async def verifier(
        goal: str,
        observation: DesktopObservation,
        history: list[dict[str, object]],
    ) -> bool:
        return bool(history)

    loop = ComputerAgentLoop(controller, max_iterations=3)
    result = await loop.run("Open Code", planner, verifier)
    assert result["ok"]
    assert result["iterations"] == 1


@pytest.mark.asyncio
async def test_scheduler_runs_once_and_can_cancel() -> None:
    bus = EventBus()
    scheduler = TaskScheduler(bus)
    result: list[str] = []

    async def job_factory() -> str:
        result.append("ran")
        return "done"

    job = scheduler.add_once("check", job_factory)
    await scheduler.start()
    try:
        for _ in range(20):
            pending = [item for item in scheduler.jobs() if item.id == job.id]
            if pending and pending[0].status == "COMPLETED":
                break
            await asyncio.sleep(0.02)
        assert result == ["ran"]
        assert scheduler.jobs()[0].status == "COMPLETED"
        assert scheduler.cancel(job.id)
    finally:
        await scheduler.stop()
    assert [str(event.type) for event in bus.history()] == [
        "BACKGROUND_TASK_STARTED",
        "BACKGROUND_TASK_COMPLETED",
    ]


@pytest.mark.asyncio
async def test_llm_planner_parses_json_plan() -> None:
    class PlannerModel:
        async def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            return ModelReply(
                content=(
                    '[{"title":"Open project","tool":"computer_open_app"},'
                    '{"title":"Run tests","tool":"run"}]'
                ),
                tool_calls=[],
                raw_message={"role": "assistant", "content": "plan"},
            )

    planner = LLMPlanner(PlannerModel())  # type: ignore[arg-type]
    steps = await planner.create_plan("Check the project")
    assert [(step.title, step.tool) for step in steps] == [
        ("Open project", "computer_open_app"),
        ("Run tests", "run"),
    ]


def test_runtime_configuration_parses_policy_and_mcp(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace=tmp_path,
        permission_auto_approve="READ_ONLY",
        permission_allowed_apps="Code.exe, chrome.exe",
        mcp_servers=(
            '[{"name":"demo","command":["python","server.py"],'
            '"env":{"TOKEN":"x"},"timeout_seconds":12}]'
        ),
    )

    policy = permission_policy_from_settings(settings)
    servers = mcp_server_configs_from_settings(settings)

    assert policy.auto_approve == {PermissionLevel.READ_ONLY}
    assert policy.allowed_apps == ("Code.exe", "chrome.exe")
    assert servers[0].name == "demo"
    assert servers[0].command == ("python", "server.py")
    assert servers[0].timeout_seconds == 12


def test_reminder_store_persists_pending_reminders(tmp_path: Path) -> None:
    path = tmp_path / "jarvis.db"
    store = ReminderStore(path)
    reminder = store.create(
        "test-reminder",
        "Persist me",
        "2030-01-01T00:00:00+00:00",
    )
    store.close()

    reopened = ReminderStore(path)
    try:
        pending = reopened.pending()
        assert [item.id for item in pending] == [reminder.id]
        updated = reopened.update_status(reminder.id, ReminderStatus.COMPLETED)
        assert updated is not None
        assert updated.status == ReminderStatus.COMPLETED
        assert reopened.pending() == []
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_toolbox_validates_arguments_and_output(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        workspace=tmp_path,
    )
    memory = MemoryStore(tmp_path / "memory.db")
    toolbox = Toolbox(settings, memory)

    async def echo(value: str) -> str:
        return value

    toolbox.register(
        Tool(
            name="echo_test",
            description="Echo.",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            handler=echo,
            permission=PermissionLevel.READ_ONLY,
        )
    )
    assert await toolbox.call("echo_test", {"value": "ok"}) == {
        "ok": True,
        "result": "ok",
    }
    missing = await toolbox.call("echo_test", {})
    assert missing["ok"] is False
    assert "InvalidArguments" in missing["error"]
    extra = await toolbox.call("echo_test", {"value": "ok", "extra": True})
    assert extra["ok"] is False
    assert "InvalidArguments" in extra["error"]

    async def bad_output() -> object:
        return object()

    toolbox.register(
        Tool(
            name="bad_output",
            description="Return an unsupported object.",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=bad_output,
            permission=PermissionLevel.READ_ONLY,
        )
    )
    invalid_output = await toolbox.call("bad_output", {})
    assert invalid_output["ok"] is False
    assert "unsupported output type" in invalid_output["error"]


@pytest.mark.asyncio
async def test_desktop_controller_verifies_app_and_clipboard_actions() -> None:
    class VerifyingDesktop:
        def __init__(self) -> None:
            self.windows: list[dict[str, object]] = []
            self.clipboard = ""

        async def screenshot(self) -> bytes:
            return b"png"

        async def window_list(self) -> list[dict[str, object]]:
            return self.windows

        async def foreground_window(self) -> dict[str, object]:
            return {"title": "Code", "process_name": "Code"}

        async def focused_value(self) -> str:
            return self.clipboard

        async def ui_tree(self, max_items: int = 120) -> list[dict[str, object]]:
            return []

        async def app_open(self, application: str) -> None:
            self.windows = [{"title": "Code"}]

        async def app_close(self, application: str) -> None:
            self.windows = []

        async def clipboard_write(self, text: str) -> None:
            self.clipboard = text

        async def clipboard_read(self) -> str:
            return self.clipboard

    permission_manager = PermissionManager(
        PermissionPolicy(auto_approve=set(PermissionLevel))
    )
    controller = DesktopController(
        VerifyingDesktop(),
        permission_manager=permission_manager,
    )
    await controller.execute("app_open", {"application": "Code.exe"})
    await controller.execute("clipboard_write", {"text": "hello"})

    class BrokenClipboard(VerifyingDesktop):
        async def clipboard_write(self, text: str) -> None:
            self.clipboard = "wrong"

    broken = DesktopController(
        BrokenClipboard(),
        permission_manager=permission_manager,
    )
    with pytest.raises(RuntimeError, match="verification failed"):
        await broken.execute("clipboard_write", {"text": "hello"})

    class BrokenOpen(VerifyingDesktop):
        async def app_open(self, application: str) -> None:
            self.windows = []

    failed_open = DesktopController(
        BrokenOpen(),
        permission_manager=permission_manager,
    )
    with pytest.raises(RuntimeError, match="verification failed"):
        await failed_open.execute(
            "app_open",
            {"application": "Code.exe"},
        )


@pytest.mark.asyncio
async def test_permission_policy_allows_normal_and_medium_but_gates_high_risk() -> None:
    manager = PermissionManager(
        PermissionPolicy(
            auto_approve={
                PermissionLevel.READ_ONLY,
                PermissionLevel.LOW_RISK,
                PermissionLevel.NORMAL,
                PermissionLevel.MEDIUM_RISK,
            }
        )
    )

    normal = await manager.decide(
        PermissionLevel.NORMAL,
        "computer_keyboard_type",
        "hello",
    )
    medium = await manager.decide(
        PermissionLevel.MEDIUM_RISK,
        "computer_keyboard_hotkey",
        "alt+f4",
    )
    high = await manager.decide(
        PermissionLevel.HIGH_RISK,
        "delete_file",
        "old.txt",
    )

    assert normal.allowed is True
    assert medium.allowed is True
    assert high.allowed is False
    assert high.requires_confirmation is True


@pytest.mark.asyncio
async def test_permission_policy_allows_only_trusted_message_recipients() -> None:
    manager = PermissionManager(
        PermissionPolicy(
            allowed_message_recipients=("文件传输助手",),
        )
    )

    trusted = await manager.decide(
        PermissionLevel.HIGH_RISK,
        "send_message",
        "文件传输助手",
    )
    unknown = await manager.decide(
        PermissionLevel.HIGH_RISK,
        "send_message",
        "张三",
    )

    assert trusted.allowed is True
    assert unknown.allowed is False
    assert unknown.requires_confirmation is True
