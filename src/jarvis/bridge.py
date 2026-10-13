from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import WSMsgType, web
from faster_whisper import WhisperModel

from jarvis.agent import Agent
from jarvis.asr_provider import create_asr_provider
from jarvis.config import Settings
from jarvis.fast_commands import FastCommandRouter, normalize_fast_command
from jarvis.memory import MemoryStore
from jarvis.persona import DynamicPersona, PersonaProfile
from jarvis.production import (
    DiagnosticsCenter,
    ProductionRuntime,
)
from jarvis.runtime import (
    AgentExecutionEngine,
    AgentTrace,
    AgentTraceStore,
    AmbientContext,
    ApprovalBroker,
    ApprovalDecision,
    AutonomousAgentLoop,
    AutonomyBoundary,
    BrowserTool,
    CancellationToken,
    ComponentHealth,
    ConfirmationManager,
    ContextEngine,
    DiagnosisEngine,
    ErrorLogger,
    EventBus,
    EventType,
    ExecutionStore,
    FileIntelligence,
    HealthChecker,
    HealthStatus,
    LLMPlanner,
    MCPManager,
    MCPToolAdapter,
    MemoryEmbeddingStore,
    MemoryExtractor,
    ModelRole,
    ModelRouter,
    MultimodalEngine,
    OperationCancelled,
    PermissionLevel,
    PermissionManager,
    PluginManager,
    ProactiveEngine,
    ReminderService,
    ReminderStore,
    RuntimeEvent,
    ScreenObserver,
    SelfRecoveryEngine,
    SystemMonitor,
    TaskEngine,
    TaskScheduler,
    TaskStep,
    TaskStore,
    TokenBudgetManager,
    ToolRegistry,
    VisionAgent,
    WorkflowEngine,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStore,
    mcp_server_configs_from_settings,
    permission_policy_from_settings,
)
from jarvis.runtime.browser import PlaywrightBrowserAdapter, WebBrowserAdapter
from jarvis.streaming_asr import (
    build_dynamic_initial_prompt,
    normalize_transcript,
)
from jarvis.tools import Tool, Toolbox
from jarvis.version import __version__
from jarvis.voice import SpeechSynthesizer
from jarvis.voice_response import StreamingVoiceResponseController, VoiceResponseController
from jarvis.device_channel import register_device_channel

KEEPALIVE_INTERVAL_SECONDS = 240


class LocalVoiceBridge:
    def __init__(self, settings: Settings) -> None:
        self.production = ProductionRuntime(settings)
        self.settings = self.production.settings
        self.settings.ensure_data_dir()
        self.memory = MemoryStore(settings.database_path)
        self.persona = DynamicPersona(settings)
        self.event_bus = EventBus()
        self.approval_broker = ApprovalBroker(self.event_bus)
        self.confirmation_manager = ConfirmationManager(self.approval_broker.request)
        self._active_voice_emit: Any = None
        self.event_bus.subscribe(
            self._on_permission_request,
            {EventType.PERMISSION_REQUEST},
        )
        self.permission_manager = PermissionManager(
            permission_policy_from_settings(settings),
            confirmation_manager=self.confirmation_manager,
        )
        self.task_store = TaskStore(settings.database_path)
        self.task_engine = TaskEngine(self.task_store, self.event_bus)
        self.scheduler = TaskScheduler(self.event_bus)
        self.reminder_store = ReminderStore(settings.database_path)
        self.reminder_service = ReminderService(
            self.reminder_store,
            self.scheduler,
            self.event_bus,
        )
        self.budget_manager = TokenBudgetManager(
            request_token_budget=settings.request_token_budget,
            task_token_budget=settings.task_token_budget,
            daily_token_budget=settings.daily_token_budget,
            input_cost_per_million=settings.input_cost_per_million,
            output_cost_per_million=settings.output_cost_per_million,
            downshift_ratio=settings.budget_downshift_ratio,
        )
        self.router = ModelRouter(
            settings,
            budget_manager=self.budget_manager,
        )
        self.mcp_manager = MCPManager(self.event_bus)
        self.mcp_configs = mcp_server_configs_from_settings(settings)
        semantic_provider = self.router.provider_for(ModelRole.CHAT)
        self.semantic_memory = MemoryEmbeddingStore(
            self.memory,
            semantic_provider,
            model_name=settings.embedding_model or settings.model,
        )
        self.memory_extractor = MemoryExtractor(
            self.memory,
            provider=semantic_provider,
            semantic_store=self.semantic_memory,
        )
        self.context_engine = ContextEngine(
            self.memory,
            self.task_engine,
            self.persona,
            semantic_store=self.semantic_memory,
            provider=self.router.provider_for(ModelRole.REASONING),
            max_context_chars=settings.context_max_chars,
        )
        self.proactive_engine = ProactiveEngine(
            self.event_bus,
            task_engine=self.task_engine,
            stale_task_seconds=settings.proactive_stale_task_minutes * 60,
            min_priority=settings.proactive_min_priority,
            quiet_hours=settings.proactive_quiet_hours,
            timezone=settings.timezone,
        )
        voice_settings = settings.model_copy(
            update={
                "model": settings.voice_model,
                "max_tokens": settings.voice_max_tokens,
                "max_iterations": settings.voice_max_iterations,
            }
        )
        vision_provider = (
            self.router.provider_for(ModelRole.VISION)
            if settings.vision_model.strip()
            else None
        )
        self.toolbox = Toolbox(
            voice_settings,
            self.memory,
            permission_manager=self.permission_manager,
            scheduler=self.scheduler,
            reminder_service=self.reminder_service,
            event_bus=self.event_bus,
            vision_provider=vision_provider,
        )
        self.fast_command_router = FastCommandRouter()
        self.tool_registry = ToolRegistry()
        self.tool_registry.register_toolbox(self.toolbox)
        browser_adapter = (
            PlaywrightBrowserAdapter(headless=settings.browser_headless)
            if settings.browser_backend == "playwright"
            else WebBrowserAdapter()
        )
        self.browser_tool = BrowserTool(browser_adapter)
        self.browser_tool.register(self.tool_registry)
        self.execution_store = ExecutionStore(settings.database_path)
        reasoning_provider = self.router.provider_for(ModelRole.REASONING)
        self.execution_engine = AgentExecutionEngine(
            store=self.execution_store,
            registry=self.tool_registry,
            permission_manager=self.permission_manager,
            planner=LLMPlanner(reasoning_provider),
            task_engine=self.task_engine,
            context_engine=self.context_engine,
            event_bus=self.event_bus,
            provider=reasoning_provider,
            budget_manager=self.budget_manager,
        )
        self.agent = Agent(
            voice_settings,
            self.memory,
            self.router.provider_for(ModelRole.VOICE),
            toolbox=self.toolbox,
            event_bus=self.event_bus,
            task_engine=self.task_engine,
            model_router=self.router,
            planner=LLMPlanner(reasoning_provider),
            persona=self.persona,
            context_engine=self.context_engine,
            memory_extractor=(
                self.memory_extractor
                if settings.memory_extraction_enabled
                else None
            ),
            tool_registry=self.tool_registry,
            budget_manager=self.budget_manager,
            execution_engine=self.execution_engine,
        )
        self.file_intelligence = FileIntelligence(reasoning_provider)
        self.file_intelligence.register(self.tool_registry)
        self.screen_observer = ScreenObserver(
            self.toolbox.desktop,
            vision_provider,
            cache_seconds=_vision_cache_seconds(settings),
        )
        self.vision_agent = VisionAgent(
            self.screen_observer,
            vision_provider,
        )
        self.vision_agent.register(self.tool_registry)
        self.multimodal_engine = MultimodalEngine(
            vision_agent=self.vision_agent,
            file_intelligence=self.file_intelligence,
            provider=vision_provider,
        )
        self.ambient_context = AmbientContext(
            screen_observer=self.screen_observer,
        )
        self.context_engine.ambient_context = self.ambient_context
        self.workflow_store = WorkflowStore(settings.database_path)
        self.workflow_engine = WorkflowEngine(
            store=self.workflow_store,
            execution_engine=self.execution_engine,
            event_bus=self.event_bus,
        )
        self.autonomous_loop = AutonomousAgentLoop(
            execution_engine=self.execution_engine,
            context_engine=self.context_engine,
            ambient_context=self.ambient_context,
            budget_manager=self.budget_manager,
        )
        plugin_dirs = [
            Path(item).expanduser()
            for item in settings.plugin_dirs.split(os.pathsep)
            if item.strip()
        ]
        self.plugin_manager = PluginManager(
            signing_key=settings.plugin_signing_key.get_secret_value(),
            allowed_roots=tuple(plugin_dirs),
        )
        for plugin_dir in plugin_dirs:
            self.plugin_manager.load_directory(plugin_dir)
        self.plugin_manager.register_tools(self.tool_registry)
        self.trace_store = AgentTraceStore(settings.database_path)
        self.agent_trace = AgentTrace(self.trace_store)
        self._trace_unsubscribe = self.agent_trace.attach(self.event_bus)
        self.system_monitor = SystemMonitor(
            self.toolbox,
            self.event_bus,
            interval_seconds=60,
        )
        self.health_checker = HealthChecker(self)
        self.diagnosis_engine = DiagnosisEngine()
        self.self_recovery_engine = SelfRecoveryEngine(self.event_bus)
        self.self_recovery_engine.register("mcp", self._recover_mcp)
        self.self_recovery_engine.register("browser", self._recover_browser)
        self.self_recovery_engine.register("vision", self._recover_vision)
        self.error_logger = ErrorLogger(self.settings.data_dir / "logs" / "errors.jsonl")
        self.diagnostics_center = DiagnosticsCenter(
            paths=self.production.paths,
            health_checker=self.health_checker,
            service_manager=self.production.service_manager,
            version=__version__,
        )

        async def execute_goal(goal: str) -> dict[str, Any]:
            record = await self.execution_engine.run(
                goal,
                session_id=f"agent-handoff-{uuid.uuid4().hex}",
            )
            return record.to_dict()

        self.toolbox.register(
            Tool(
                name="execute_goal",
                description=(
                    "Execute a complex multi-step goal through the bounded Agent "
                    "Execution Engine. Use this when a task requires several tools, "
                    "observation, verification, and recovery."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "string",
                            "description": "The complete user goal to execute.",
                        }
                    },
                    "required": ["goal"],
                    "additionalProperties": False,
                },
                handler=execute_goal,
                permission=PermissionLevel.LOW_RISK,
                capability="task",
                category="TASK",
                metadata={"agent_handoff": True},
            )
        )
        self.tool_registry.register_toolbox(self.toolbox)
        self._execution_tasks: dict[str, asyncio.Task[None]] = {}
        self._execution_cancellations: dict[str, CancellationToken] = {}
        self._workflow_tasks: dict[str, asyncio.Task[None]] = {}
        self._workflow_cancellations: dict[str, CancellationToken] = {}
        self._autonomous_tasks: dict[str, asyncio.Task[None]] = {}
        self._autonomous_cancellations: dict[str, CancellationToken] = {}
        self.whisper = WhisperModel(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            cpu_threads=min(os.cpu_count() or 4, 8),
            num_workers=1,
        )
        self.whisper_partial = (
            self.whisper
            if settings.whisper_partial_model == settings.whisper_model
            else WhisperModel(
                settings.whisper_partial_model,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
                cpu_threads=min(os.cpu_count() or 4, 8),
                num_workers=1,
            )
        )
        self._lock = asyncio.Lock()
        self._synthesizer_lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task[None] | None = None
        self._cancellations: dict[str, CancellationToken] = {}
        self._active_voice_request_id: str | None = None
        self._speech_synthesizer: SpeechSynthesizer | None = None
        self._llm_warmup_error = ""
        self.production.apply_permission_policy(self.permission_manager)
        self._register_production_services()
        self._configure_production_launcher()
        self.production.migrate_database()
        self._attach_recovery_tasks()
        if self.production.paths.environment == "production":
            self.production.backup_manager.create_if_due()
        self.production.log_manager.log(
            "INFO",
            "startup",
            "Production runtime initialized.",
            metadata={
                "version": __version__,
                "environment": str(self.production.paths.environment),
                "pending_restore": bool(self.production.pending_restore),
            },
        )

    def _register_production_services(self) -> None:
        manager = self.production.service_manager

        async def healthy(name: str, details: dict[str, Any] | None = None) -> ComponentHealth:
            return ComponentHealth(
                name=name,
                status=HealthStatus.HEALTHY,
                details=details or {},
            )

        async def llm_health() -> ComponentHealth:
            provider = self.router.provider_for("chat")
            return ComponentHealth(
                name="llm",
                status=(
                    HealthStatus.HEALTHY
                    if provider is not None and not self.settings.offline_mode
                    else HealthStatus.OFFLINE
                ),
                message=(
                    "Offline mode is enabled."
                    if self.settings.offline_mode
                    else ""
                ),
                details={"provider": getattr(provider, "name", "unknown")},
            )

        async def asr_health() -> ComponentHealth:
            return ComponentHealth(
                name="asr",
                status=(
                    HealthStatus.HEALTHY
                    if self.whisper is not None
                    else HealthStatus.ERROR
                ),
                details={"model": self.settings.whisper_model},
            )

        async def tts_health() -> ComponentHealth:
            synthesizer = self._speech_synthesizer
            if synthesizer is None:
                synthesizer = await asyncio.to_thread(self._get_speech_synthesizer)
            return ComponentHealth(
                name="tts",
                status=(
                    HealthStatus.HEALTHY
                    if synthesizer is not None
                    else HealthStatus.ERROR
                ),
                details={"voice": self.settings.tts_voice},
            )

        async def mcp_health() -> ComponentHealth:
            return await self.health_checker._check_mcp()

        async def agent_health() -> ComponentHealth:
            try:
                self.execution_engine.store.list(1)
                return await healthy("agent")
            except Exception as exc:
                return ComponentHealth(
                    name="agent",
                    status=HealthStatus.ERROR,
                    message=f"{type(exc).__name__}: {exc}",
                )

        async def bridge_health() -> ComponentHealth:
            return await healthy(
                "bridge",
                {"version": __version__, "environment": str(self.production.paths.environment)},
            )

        async def no_op() -> None:
            return None

        async def restart_mcp() -> None:
            await self._recover_mcp(None)

        manager.register(
            "bridge",
            health=bridge_health,
            start=no_op,
            stop=no_op,
            critical=True,
            max_restarts=1,
        )
        manager.register(
            "llm",
            health=llm_health,
            start=no_op,
            stop=no_op,
            critical=False,
            max_restarts=3,
        )
        manager.register(
            "asr",
            health=asr_health,
            start=no_op,
            stop=no_op,
            critical=True,
            max_restarts=2,
        )
        manager.register(
            "tts",
            health=tts_health,
            start=no_op,
            stop=no_op,
            critical=True,
            max_restarts=2,
        )
        manager.register(
            "mcp",
            health=mcp_health,
            restart=restart_mcp,
            start=no_op,
            stop=no_op,
            critical=False,
            max_restarts=3,
        )
        manager.register(
            "agent",
            health=agent_health,
            start=no_op,
            stop=no_op,
            critical=True,
            max_restarts=1,
        )

    def _configure_production_launcher(self) -> None:
        async def check_database() -> None:
            self.memory.list_memories(1)
            self.task_engine.list_tasks(1)

        async def check_services() -> None:
            await self.production.service_manager.start_all()

        async def check_agent() -> None:
            if not self.tool_registry.all():
                raise RuntimeError("No tools are registered.")

        self.production.launcher.add_stage("database", check_database)
        self.production.launcher.add_stage("services", check_services)
        self.production.launcher.add_stage("agent", check_agent)

    def _attach_recovery_tasks(self) -> None:
        active_statuses = {
            "CREATED",
            "PLANNING",
            "EXECUTING",
            "WAITING_CONFIRMATION",
            "VERIFYING",
            "PAUSED",
        }
        tasks = [
            task.to_dict()
            for task in self.task_engine.list_tasks(100)
            if str(task.status) in active_statuses
        ]
        self.production.attach_unfinished_tasks(tasks)
        if self.production.recovery_report.unclean_shutdown and tasks:
            self.production.log_manager.log(
                "WARN",
                "recovery",
                "Previous shutdown was abnormal and unfinished tasks were found.",
                metadata={"task_count": len(tasks)},
            )

    async def handle_health(self, _request: web.Request) -> web.Response:
        capabilities = ["chat", "tools", "memory", "cancel", "events"]
        if self.settings.vision_model.strip():
            capabilities.append("vision")
        return web.json_response(
            {
                "ok": True,
                "model": self.settings.voice_model,
                "provider": self.settings.model_base_url,
                "capabilities": capabilities,
            }
        )

    async def handle_health_details(self, _request: web.Request) -> web.Response:
        self._authorize_request(_request)
        health = await self.health_checker.check()
        trace = self.trace_store.list(limit=500)
        try:
            snapshot = (await self.system_monitor.sample()).to_dict()
        except Exception:
            snapshot = {}
        diagnoses = self.diagnosis_engine.diagnose(
            health,
            trace=trace,
            metrics=snapshot,
        )
        return web.json_response(
            {
                "ok": health.status.value in {"HEALTHY", "DEGRADED"},
                "health": health.to_dict(),
                "diagnoses": [item.to_dict() for item in diagnoses],
                "system": snapshot,
            }
        )

    def _authorize_production_admin(self, request: web.Request) -> None:
        self._authorize_request(request)
        owner_id = str(
            request.headers.get("X-Jarvis-Owner")
            or request.query.get("owner_id")
            or "local"
        ).strip()
        if owner_id != "local":
            raise web.HTTPForbidden(
                text="Production administration is restricted to the local owner."
            )

    async def handle_production_snapshot(self, request: web.Request) -> web.Response:
        self._authorize_production_admin(request)
        return web.json_response({"ok": True, "production": self.production.snapshot()})

    async def handle_production_config(self, request: web.Request) -> web.Response:
        self._authorize_production_admin(request)
        if request.method == "GET":
            return web.json_response(
                {
                    "ok": True,
                    "config": self.production.config_manager.public_settings(),
                    "secrets": self.production.secret_manager.status(),
                }
            )
        payload = await request.json()
        updates = (
            {str(payload["path"]): payload.get("value")}
            if isinstance(payload.get("path"), str)
            else payload
        )
        updates = {
            path: value
            for path, value in updates.items()
            if _allowed_config_path(path)
        }
        if not updates:
            raise web.HTTPBadRequest(text="No supported configuration values were supplied.")
        self.production.config_manager.update(updates)
        self.production.log_manager.log(
            "INFO",
            "configuration",
            "Configuration updated.",
            metadata={"keys": sorted(updates)},
        )
        return web.json_response(
            {
                "ok": True,
                "restart_required": True,
                "config": self.production.config_manager.public_settings(),
                "secrets": self.production.secret_manager.status(),
            }
        )

    async def handle_production_permissions(self, request: web.Request) -> web.Response:
        self._authorize_production_admin(request)
        if request.method == "GET":
            return web.json_response(
                {"ok": True, "permissions": self.production.permission_center.to_dict()}
            )
        payload = await request.json()
        category = str(payload.get("category") or "").strip()
        mode = str(payload.get("mode") or "").strip()
        if not category or not mode:
            raise web.HTTPBadRequest(text="category and mode are required.")
        try:
            modes = self.production.permission_center.set_mode(category, mode)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        self.production.apply_permission_policy(self.permission_manager)
        return web.json_response(
            {
                "ok": True,
                "permissions": self.production.permission_center.to_dict(),
                "modes": modes,
            }
        )

    async def handle_production_logs(self, request: web.Request) -> web.Response:
        self._authorize_production_admin(request)
        limit = max(1, min(int(request.query.get("limit", "200")), 2_000))
        records = self.production.log_manager.query(
            level=request.query.get("level"),
            module=request.query.get("module"),
            trace_id=request.query.get("trace_id"),
            task_id=request.query.get("task_id"),
            limit=limit,
        )
        return web.json_response(
            {
                "ok": True,
                "logs": records,
                "stats": self.production.log_manager.stats(),
            }
        )

    async def handle_production_data(self, request: web.Request) -> web.Response:
        self._authorize_production_admin(request)
        section = request.query.get("section", "overview").strip().lower()
        query = request.query.get("query", "").strip()
        limit = max(1, min(int(request.query.get("limit", "100")), 1_000))
        if request.method == "POST" and section != "cleanup":
            raise web.HTTPMethodNotAllowed(request.method, ["GET"])
        if section == "cleanup":
            return await self._handle_data_cleanup(request)
        if section == "memory":
            owner_id = request.headers.get("X-Jarvis-Owner", "local")
            memories = (
                await self.semantic_memory.search(query, limit, owner_id=owner_id)
                if query
                else self.memory.list_memories(limit, owner_id=owner_id)
            )
            return web.json_response(
                {"ok": True, "items": [_memory_payload(memory) for memory in memories]}
            )
        if section == "tasks":
            tasks = self.task_engine.list_tasks(limit)
            return web.json_response(
                {"ok": True, "items": [task.to_dict() for task in tasks]}
            )
        if section == "conversations":
            return web.json_response(
                {
                    "ok": True,
                    "items": self.memory.list_sessions(limit, search=query),
                }
            )
        if section == "logs":
            return web.json_response(
                {
                    "ok": True,
                    "items": self.production.log_manager.query(
                        module=query or None,
                        limit=limit,
                    ),
                }
            )
        if section == "backups":
            return web.json_response(
                {
                    "ok": True,
                    "items": [
                        record.to_dict()
                        for record in self.production.backup_manager.list_backups()
                    ],
                }
            )
        overview = self._data_overview()
        return web.json_response({"ok": True, "overview": overview})

    async def _handle_data_cleanup(self, request: web.Request) -> web.Response:
        payload = await request.json()
        if not bool(payload.get("confirmed")):
            raise web.HTTPBadRequest(text="confirmed=true is required for cleanup.")
        removed: dict[str, int] = {}
        if bool(payload.get("cache", True)):
            removed["cache_files"] = self._clear_cache()
        if bool(payload.get("expired_memory", True)):
            removed["expired_memories"] = self.memory.expire_memories()
        if bool(payload.get("logs", False)):
            removed["log_files"] = self.production.log_manager.cleanup()
        return web.json_response({"ok": True, "removed": removed})

    async def handle_production_data_export(self, request: web.Request) -> web.Response:
        self._authorize_production_admin(request)
        payload = await request.json()
        section = str(payload.get("section") or "overview")
        content = {
            "exported_at": time.time(),
            "environment": str(self.production.paths.environment),
            "section": section,
            "data": self._data_overview() if section == "overview" else {},
        }
        path = self.production.paths.exports / (
            f"jarvis-data-{section}-{int(time.time())}.json"
        )
        path.write_text(
            json.dumps(content, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return web.json_response({"ok": True, "path": str(path)})

    def _data_overview(self) -> dict[str, Any]:
        sessions = self.memory.list_sessions(1_000)
        tasks = self.task_engine.list_tasks(1_000)
        backups = self.production.backup_manager.list_backups()
        cache_bytes = sum(
            path.stat().st_size
            for path in self.production.paths.cache.rglob("*")
            if path.is_file()
        ) if self.production.paths.cache.exists() else 0
        return {
            "memory_count": len(self.memory.list_memories(1_000)),
            "task_count": len(tasks),
            "active_task_count": sum(
                1 for task in tasks if str(task.status) not in {"COMPLETED", "CANCELLED"}
            ),
            "conversation_count": len(sessions),
            "message_count": sum(int(item["message_count"]) for item in sessions),
            "log": self.production.log_manager.stats(),
            "backup_count": len(backups),
            "latest_backup": backups[0].to_dict() if backups else None,
            "cache_size_bytes": cache_bytes,
            "database_size_bytes": (
                self.production.paths.database.stat().st_size
                if self.production.paths.database.exists()
                else 0
            ),
        }

    def _clear_cache(self) -> int:
        if not self.production.paths.cache.exists():
            return 0
        removed = 0
        for path in self.production.paths.cache.rglob("*"):
            if not path.is_file():
                continue
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    async def handle_production_diagnostics(self, request: web.Request) -> web.Response:
        self._authorize_production_admin(request)
        report = await self.diagnostics_center.collect()
        if request.method == "POST" and request.query.get("export") == "1":
            path = self.diagnostics_center.export(report)
            return web.json_response(
                {
                    "ok": True,
                    "report": report.to_dict(),
                    "export_path": str(path),
                }
            )
        return web.json_response({"ok": True, "report": report.to_dict()})

    async def handle_production_backups(self, request: web.Request) -> web.Response:
        self._authorize_production_admin(request)
        if request.method == "GET":
            return web.json_response(
                {
                    "ok": True,
                    "backups": [
                        record.to_dict()
                        for record in self.production.backup_manager.list_backups()
                    ],
                }
            )
        payload = await request.json()
        record = self.production.backup_manager.create(
            kind=str(payload.get("kind") or "manual"),
            label=str(payload.get("label") or ""),
        )
        return web.json_response({"ok": True, "backup": record.to_dict()})

    async def handle_production_restore(self, request: web.Request) -> web.Response:
        self._authorize_production_admin(request)
        payload = await request.json()
        archive = Path(str(payload.get("path") or "")).expanduser().resolve()
        allowed_root = self.production.paths.backups.resolve()
        if allowed_root not in archive.parents:
            raise web.HTTPBadRequest(text="Only managed backup archives can be restored.")
        if not bool(payload.get("confirmed")):
            raise web.HTTPBadRequest(text="confirmed=true is required for restore.")
        self.permission_manager.grant_once("backup_restore", str(archive))
        await self.permission_manager.require(
            PermissionLevel.HIGH_RISK,
            "backup_restore",
            str(archive),
            metadata={"category": "system"},
        )
        result = self.production.restore_backup(
            archive,
            confirmed=True,
        )
        return web.json_response(result)

    async def handle_production_recovery(self, request: web.Request) -> web.Response:
        self._authorize_production_admin(request)
        if request.method == "GET":
            return web.json_response(
                {"ok": True, "recovery": self.production.recovery_report.to_dict()}
            )
        payload = await request.json()
        action = str(payload.get("action") or "").strip().lower()
        task_ids = [str(item) for item in payload.get("task_ids") or []]
        if action == "dismiss":
            return web.json_response({"ok": True, "action": "dismissed"})
        if action != "resume":
            raise web.HTTPBadRequest(text="action must be resume or dismiss.")
        resumed: list[str] = []
        for task_id in task_ids:
            task = self.task_engine.get_task(task_id)
            if task is None:
                continue
            await self.task_engine.resume_task(task_id)
            resumed.append(task_id)
        return web.json_response({"ok": True, "resumed": resumed})

    async def handle_production_services(self, request: web.Request) -> web.Response:
        self._authorize_production_admin(request)
        manager = self.production.service_manager
        if request.method == "GET":
            return web.json_response({"ok": True, "services": manager.snapshot()})
        name = request.match_info["name"]
        action = request.match_info["action"]
        if name not in {item["name"] for item in manager.snapshot()}:
            raise web.HTTPNotFound(text="Unknown service.")
        if action == "check":
            result = await manager.health_check(name)
        elif action == "restart":
            self.permission_manager.grant_once(f"service_restart:{name}")
            decision = await self.permission_manager.require(
                PermissionLevel.NORMAL,
                f"service_restart:{name}",
                metadata={"category": "system"},
            )
            if not decision.allowed:
                raise web.HTTPForbidden(text=decision.reason)
            result = await manager.restart(name)
        elif action in {"start", "stop"}:
            result = await getattr(manager, action)(name)
        else:
            raise web.HTTPBadRequest(text="Unknown service action.")
        return web.json_response({"ok": True, "service": result})

    async def handle_production_launcher(self, request: web.Request) -> web.Response:
        self._authorize_production_admin(request)
        if request.method == "POST":
            await self.production.launcher.run(self.health_checker.check)
        return web.json_response(
            {"ok": True, "launcher": self.production.launcher.status.to_dict()}
        )

    async def handle_production_update(self, request: web.Request) -> web.Response:
        self._authorize_production_admin(request)
        manager = self.production.update_manager
        if request.method == "GET":
            return web.json_response({"ok": True, "update": manager.snapshot()})
        payload = await request.json()
        action = str(payload.get("action") or "").strip().lower()
        if action == "check":
            url = str(
                payload.get("url")
                or self.settings.update_manifest_url
                or self.production.config_manager.get("updates.manifest_url")
                or ""
            ).strip()
            if not url:
                raise web.HTTPBadRequest(text="No update manifest URL is configured.")
            result = await manager.check(
                url,
                channel=str(
                    payload.get("channel")
                    or self.settings.update_channel
                    or "stable"
                ),
            )
        elif action == "download":
            result = await manager.download()
        elif action == "install":
            confirmed = bool(payload.get("confirmed"))
            if not confirmed:
                raise web.HTTPBadRequest(
                    text="confirmed=true is required for update installation."
                )
            self.permission_manager.grant_once("update_install")
            await self.permission_manager.require(
                PermissionLevel.HIGH_RISK,
                "update_install",
                metadata={"category": "system"},
            )
            result = manager.install(confirmed=True)
        elif action == "rollback":
            confirmed = bool(payload.get("confirmed"))
            if not confirmed:
                raise web.HTTPBadRequest(
                    text="confirmed=true is required for update rollback."
                )
            self.permission_manager.grant_once("update_rollback")
            await self.permission_manager.require(
                PermissionLevel.HIGH_RISK,
                "update_rollback",
                metadata={"category": "system"},
            )
            backup_path = (
                Path(str(payload["backup_path"])).expanduser().resolve()
                if payload.get("backup_path")
                else None
            )
            result = manager.rollback(
                confirmed=True,
                backup_path=backup_path,
            )
        else:
            raise web.HTTPBadRequest(text="Unknown update action.")
        return web.json_response({"ok": True, "update": result})

    async def handle_production_version(self, request: web.Request) -> web.Response:
        self._authorize_production_admin(request)
        return web.json_response(
            {
                "ok": True,
                "version": __version__,
                "environment": str(self.production.paths.environment),
            }
        )

    async def handle_self_recovery(self, _request: web.Request) -> web.Response:
        self._authorize_request(_request)
        health = await self.health_checker.check()
        diagnoses = self.diagnosis_engine.diagnose(health)
        results = [
            await self.self_recovery_engine.recover(diagnosis)
            for diagnosis in diagnoses
        ]
        return web.json_response(
            {
                "ok": True,
                "results": results,
            }
        )

    def _request_scope(
        self,
        request: web.Request,
        payload: dict[str, Any] | None = None,
    ) -> tuple[str, str | None]:
        payload = payload or {}
        owner_id = str(
            request.headers.get("X-Jarvis-Owner")
            or request.query.get("owner_id")
            or payload.get("owner_id")
            or "local"
        ).strip() or "local"
        session_id = str(
            request.headers.get("X-Jarvis-Session")
            or request.query.get("session_id")
            or payload.get("session_id")
            or ""
        ).strip() or None
        return owner_id, session_id

    def _asr_initial_prompt(self, owner_id: str) -> str:
        base = self.settings.whisper_initial_prompt
        try:
            memories = self.memory.list_memories(32, owner_id=owner_id)
        except Exception:
            return base
        return build_dynamic_initial_prompt(
            base,
            [memory.summary or memory.content for memory in memories],
        )

    def _log_voice_latency(self, latency: dict[str, Any]) -> None:
        self.production.log_manager.log(
            "INFO",
            "voice",
            "[VOICE] "
            f"path={latency.get('path', 'llm')} "
            f"ASR={latency.get('asr_ms', 'n/a')}ms "
            f"Router={latency.get('router_ms', 'n/a')}ms "
            f"Tool={latency.get('tool_ms', 'n/a')}ms "
            f"LLM TTFT={latency.get('llm_ttft_ms', 'n/a')}ms "
            f"TTS first audio={latency.get('tts_ttfa_ms', 'n/a')}ms "
            f"FirstAudio={latency.get('first_audio_ms', 'n/a')}ms "
            f"TOTAL={latency.get('total_ms', 'n/a')}ms",
            metadata=latency,
        )

    def _authorize_request(self, request: web.Request) -> None:
        configured = self.settings.bridge_token.get_secret_value().strip()
        if not configured:
            return
        provided = (
            request.headers.get("X-Jarvis-Token", "").strip()
            or request.query.get("token", "").strip()
        )
        authorization = request.headers.get("Authorization", "").strip()
        if authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
        if not provided or not hmac.compare_digest(provided, configured):
            raise web.HTTPUnauthorized(text="Invalid JARVIS bridge token.")

    def _memory_in_scope(
        self,
        memory_id: int,
        *,
        owner_id: str,
        session_id: str | None,
    ) -> bool:
        memory = self.memory.get_memory(memory_id)
        if memory is None or memory.owner_id != owner_id:
            return False
        return memory.session_id is None or session_id is None or memory.session_id == session_id

    async def handle_memory_list(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        owner_id, session_id = self._request_scope(request)
        query = request.query.get("query", "").strip()
        limit = max(1, min(int(request.query.get("limit", "50")), 200))
        if query:
            memories = await self.semantic_memory.search(
                query,
                limit,
                owner_id=owner_id,
                session_id=session_id,
            )
        else:
            memories = self.memory.list_memories(
                limit,
                owner_id=owner_id,
                session_id=session_id,
            )
        return web.json_response(
            {
                "ok": True,
                "memories": [_memory_payload(memory) for memory in memories],
                "semantic_error": self.semantic_memory.last_error or None,
            }
        )

    async def handle_memory_create(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        payload = await request.json()
        owner_id, session_id = self._request_scope(request, payload)
        content = str(payload.get("content") or "").strip()
        if not content:
            raise web.HTTPBadRequest(text="content is required.")
        try:
            memory = self.memory.remember(
                content,
                str(payload.get("category") or "general"),
                importance=int(payload.get("importance") or 3),
                confidence=float(payload.get("confidence") or 1.0),
                summary=str(payload.get("summary") or content),
                metadata=dict(payload.get("metadata") or {}),
                session_id=session_id,
                owner_id=owner_id,
                task_id=payload.get("task_id"),
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        await self.semantic_memory.index_memory(memory)
        return web.json_response({"ok": True, "memory": _memory_payload(memory)})

    async def handle_memory_update(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        memory_id = int(request.match_info["memory_id"])
        payload = await request.json()
        owner_id, session_id = self._request_scope(request, payload)
        if not self._memory_in_scope(
            memory_id,
            owner_id=owner_id,
            session_id=session_id,
        ):
            raise web.HTTPNotFound(text="Memory not found.")
        try:
            memory = self.memory.update_memory(
                memory_id,
                content=payload.get("content"),
                category=payload.get("category"),
                importance=(
                    int(payload["importance"])
                    if payload.get("importance") is not None
                    else None
                ),
                confidence=(
                    float(payload["confidence"])
                    if payload.get("confidence") is not None
                    else None
                ),
                summary=payload.get("summary"),
                metadata=(
                    dict(payload["metadata"])
                    if isinstance(payload.get("metadata"), dict)
                    else None
                ),
                session_id=session_id,
                owner_id=owner_id,
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if memory is None:
            raise web.HTTPNotFound(text="Memory not found.")
        await self.semantic_memory.index_memory(memory)
        return web.json_response({"ok": True, "memory": _memory_payload(memory)})

    async def handle_memory_delete(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        memory_id = int(request.match_info["memory_id"])
        owner_id, session_id = self._request_scope(request)
        if not self._memory_in_scope(
            memory_id,
            owner_id=owner_id,
            session_id=session_id,
        ):
            raise web.HTTPNotFound(text="Memory not found.")
        deleted = self.memory.forget(memory_id)
        return web.json_response({"ok": True, "deleted": deleted})

    async def handle_context(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        owner_id, session_id = self._request_scope(request)
        query = request.query.get("query", "").strip() or "当前任务 用户偏好"
        task_id = request.query.get("task_id") or None
        if not session_id:
            raise web.HTTPBadRequest(text="session_id is required for context.")
        snapshot = await self.context_engine.build_context(
            query,
            session_id=session_id,
            task_id=task_id,
            owner_id=owner_id,
        )
        return web.json_response({"ok": True, "context": snapshot.to_dict()})

    async def handle_persona(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        if request.method == "GET":
            return web.json_response(
                {"ok": True, "persona": self.persona.profile.model_dump(mode="json")}
            )
        payload = await request.json()
        profile = PersonaProfile.model_validate(
            {
                **self.persona.profile.model_dump(mode="json"),
                **payload,
            }
        )
        self.persona.profile = profile
        self.persona.store.save(profile)
        return web.json_response(
            {"ok": True, "persona": profile.model_dump(mode="json")}
        )

    async def warmup(self, _app: web.Application) -> None:
        await self._warmup_model()
        await self._connect_mcp()
        await asyncio.to_thread(self._get_speech_synthesizer)
        if self.settings.background_tasks_enabled:
            await self.scheduler.start()
            await self.reminder_service.start()
            if self.settings.proactive_enabled:
                await self.proactive_engine.start()
        await self.system_monitor.start()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        await self.production.launcher.run(self.health_checker.check)

    async def _connect_mcp(self) -> None:
        for config in self.mcp_configs:
            try:
                await self.mcp_manager.connect(config)
                self.toolbox.register_mcp_tools(self.mcp_manager)
                MCPToolAdapter(self.mcp_manager).register_tools(
                    self.tool_registry
                )
            except Exception as exc:
                print(f"MCP server {config.name} unavailable: {exc}")

    async def _recover_mcp(self, _diagnosis: Any) -> dict[str, Any]:
        recovered: list[str] = []
        for config in self.mcp_configs:
            try:
                await self.mcp_manager.connect(config)
                self.toolbox.register_mcp_tools(self.mcp_manager)
                MCPToolAdapter(self.mcp_manager).register_tools(
                    self.tool_registry
                )
                recovered.append(config.name)
            except Exception:
                continue
        if not recovered:
            raise RuntimeError("No configured MCP server could be reconnected.")
        return {"servers": recovered}

    async def _recover_browser(self, _diagnosis: Any) -> dict[str, Any]:
        await self.browser_tool.close()
        browser_adapter = (
            PlaywrightBrowserAdapter(headless=self.settings.browser_headless)
            if self.settings.browser_backend == "playwright"
            else WebBrowserAdapter()
        )
        self.browser_tool = BrowserTool(browser_adapter)
        self.browser_tool.register(self.tool_registry)
        return {"backend": self.settings.browser_backend}

    async def _recover_vision(self, _diagnosis: Any) -> dict[str, Any]:
        self.screen_observer._cache = None
        return {"cache": "cleared"}

    async def _warmup_model(self) -> None:
        if self.settings.offline_mode and not _is_local_endpoint(
            self.settings.model_base_url
        ):
            return
        base_url = self.settings.model_base_url.rstrip("/")
        try:
            if "127.0.0.1" not in base_url and "localhost" not in base_url:
                await self.agent.model.complete(
                    [{"role": "user", "content": "Reply with OK."}],
                    [],
                )
            else:
                api_url = base_url.removesuffix("/v1") + "/api/chat"
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            api_url,
                            json={
                                "model": self.settings.voice_model,
                                "messages": [
                                    {"role": "user", "content": "Reply with OK."}
                                ],
                                "stream": False,
                                "think": False,
                                "keep_alive": self.settings.keep_alive,
                                "options": {"num_predict": 24},
                            },
                            timeout=aiohttp.ClientTimeout(total=180),
                        ) as response:
                            response.raise_for_status()
                except Exception:
                    await self.agent.model.complete(
                        [{"role": "user", "content": "Reply with OK."}],
                        [],
                    )
        except Exception as exc:
            self._llm_warmup_error = f"{type(exc).__name__}: {exc}"
            self.production.log_manager.log(
                "WARN",
                "startup",
                "LLM endpoint is unavailable; starting in degraded mode.",
                metadata={"error": self._llm_warmup_error},
            )
            return
        self._llm_warmup_error = ""

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_SECONDS)
            try:
                await self._warmup_model()
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    async def handle_respond(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        async with self._lock:
            payload = await request.json()
            if self.settings.offline_mode and not _is_local_endpoint(
                self.settings.model_base_url
            ):
                raise web.HTTPServiceUnavailable(
                    text="当前 AI 服务不可用。离线模式下无法调用云端模型。"
                )
            audio_base64 = payload.get("audio_base64")
            text = payload.get("text")
            request_id = str(payload.get("request_id") or uuid.uuid4().hex)
            cancellation = CancellationToken()
            self._cancellations[request_id] = cancellation

            try:
                transcript = text or await self._transcribe_base64(audio_base64)
                cancellation.raise_if_cancelled()
                reply = await self.agent.ask(
                    transcript,
                    session_id=payload.get("session_id"),
                    cancellation=cancellation,
                    task_id=request_id,
                )
                cancellation.raise_if_cancelled()
                synthesizer = await self._get_synthesizer()
                audio_bytes, audio_sample_rate = await asyncio.to_thread(
                    synthesizer.synthesize_wav_bytes,
                    reply.text,
                )

                return web.json_response(
                    {
                        "ok": True,
                        "request_id": request_id,
                        "task_id": reply.task_id,
                        "transcript": transcript,
                        "reply": reply.text,
                        "tool_calls": reply.tool_calls,
                        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
                        "audio_format": "wav",
                        "audio_sample_rate": audio_sample_rate,
                    }
                )
            except OperationCancelled:
                return web.json_response(
                    {
                        "ok": True,
                        "cancelled": True,
                        "request_id": request_id,
                        "transcript": text or "",
                        "reply": "已取消当前任务。",
                        "tool_calls": 0,
                    }
                )
            finally:
                self._cancellations.pop(request_id, None)

    _VOICE_ALLOW_WORDS = {
        "可以", "行", "好的", "好", "好呀", "嗯", "确认", "批准", "同意", "允许",
        "没问题", "ok", "okay", "yes", "sure", "go", "go ahead", "approve",
        "allow", "yep", "yeah", "fine",
    }
    _VOICE_DENY_WORDS = {
        "不行", "不可以", "不要", "不用", "别", "取消", "算了", "拒绝", "不好",
        "否", "no", "nope", "cancel", "stop", "deny", "abort",
    }

    def _on_permission_request(self, event: RuntimeEvent) -> None:
        """Speak a confirmation question whenever an action waits for approval."""
        data = event.data or {}
        if str(data.get("id") or ""):
            asyncio.create_task(
                self._announce_permission_request(str(data.get("action") or ""))
            )

    async def _announce_permission_request(self, action: str) -> None:
        emit = self._active_voice_emit
        if emit is None:
            return
        question = "这个操作需要你的确认，说「可以」或者「不行」。"
        try:
            synthesizer = await self._get_synthesizer()
            async for audio, audio_format, sample_rate in synthesizer.stream_audio(
                question
            ):
                await emit(
                    {
                        "type": "audio_chunk",
                        "audio_base64": base64.b64encode(audio).decode("ascii"),
                        "audio_format": audio_format,
                        "audio_sample_rate": sample_rate,
                    }
                )
        except Exception:
            pass

    async def _resolve_voice_decision(self, transcript: str) -> str | None:
        """Resolve the oldest pending permission from a spoken confirmation/denial.

        Never raises: a decision check must not abort a normal turn.
        """
        try:
            pending = await self.approval_broker.pending()
            if not pending:
                return None
            normalized = normalize_fast_command(transcript).strip()
            for suffix in ("吧", "啊", "嘛", "呀", "哦", "啦"):
                if normalized.endswith(suffix) and len(normalized) > 1:
                    normalized = normalized[: -len(suffix)]
            if normalized in self._VOICE_ALLOW_WORDS:
                decision = ApprovalDecision.ALLOW_ONCE
            elif normalized in self._VOICE_DENY_WORDS:
                decision = ApprovalDecision.DENY
            else:
                return None
            try:
                resolved = await self.approval_broker.resolve(pending[0].id, decision)
            except ValueError:
                resolved = False
            if not resolved:
                return None
            return "allow" if decision == ApprovalDecision.ALLOW_ONCE else "deny"
        except Exception:
            return None

    async def handle_respond_stream(self, request: web.Request) -> web.StreamResponse:
        self._authorize_request(request)
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "application/x-ndjson",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        payload = await request.json()
        audio_base64 = payload.get("audio_base64")
        text = payload.get("text")
        request_id = str(payload.get("request_id") or uuid.uuid4().hex)
        cancellation = CancellationToken()
        self._cancellations[request_id] = cancellation
        full_text = ""
        event_lock = asyncio.Lock()

        async def emit_event(payload: dict[str, Any]) -> None:
            async with event_lock:
                await _write_stream_event(response, payload)

        turn_started_at = time.perf_counter()
        llm_started_at = turn_started_at
        llm_first_token_at: float | None = None
        tts_started_at: float | None = None
        tts_first_audio_at: float | None = None
        controller: VoiceResponseController | None = None

        try:
            transcript = text or await self._transcribe_base64(audio_base64)
            decision = await self._resolve_voice_decision(transcript)
            if decision is not None:
                acknowledgement = (
                    "好的，已确认。" if decision == "allow" else "好的，已取消。"
                )
                await emit_event({"type": "transcript", "text": transcript})
                await emit_event({"type": "text_delta", "delta": acknowledgement})
                await emit_event(
                    {
                        "type": "done",
                        "reply": acknowledgement,
                        "tool_calls": 0,
                        "audio_chunks": 0,
                        "latency": {
                            "path": "voice_decision",
                            "total_ms": round(
                                (time.perf_counter() - turn_started_at) * 1_000,
                                3,
                            ),
                        },
                    },
                )
                return
            previous_request_id = self._active_voice_request_id
            if previous_request_id and previous_request_id != request_id:
                previous = self._cancellations.get(previous_request_id)
                if previous is not None:
                    previous.cancel("Superseded by a newer voice response.")
            self._active_voice_request_id = request_id
            self._active_voice_emit = emit_event
            self.event_bus.emit(
                RuntimeEvent(
                    type=EventType.ASR_FINAL,
                    request_id=request_id,
                    data={
                        "elapsed_ms": round(
                            (time.perf_counter() - turn_started_at) * 1_000,
                            3,
                        )
                    },
                )
            )
            await emit_event(
                {"type": "transcript", "text": transcript},
            )
            spoken_char_budget = self.persona.spoken_char_budget(
                transcript,
                self.settings.voice_max_spoken_chars,
            )
            synthesizer = await self._get_synthesizer()

            async def emit_voice_event(payload: dict[str, Any]) -> None:
                nonlocal llm_first_token_at, tts_started_at, tts_first_audio_at
                event_type = str(payload.get("type") or "")
                if event_type == "synthesis_started":
                    if tts_started_at is None:
                        tts_started_at = time.perf_counter()
                    self.event_bus.emit(
                        RuntimeEvent(
                            type=EventType.TTS_START,
                            request_id=request_id,
                            data={
                                "elapsed_ms": round(
                                    (tts_started_at - llm_started_at) * 1_000,
                                    3,
                                )
                            },
                        )
                    )
                    return
                if event_type == "audio_started":
                    if tts_first_audio_at is None:
                        tts_first_audio_at = time.perf_counter()
                    self.event_bus.emit(
                        RuntimeEvent(
                            type=EventType.TTS_FIRST_AUDIO,
                            request_id=request_id,
                            data={
                                "ttfa_ms": round(
                                    (tts_first_audio_at - tts_started_at) * 1_000,
                                    3,
                                )
                                if tts_started_at is not None
                                else None
                            },
                        )
                    )
                    self.event_bus.emit(
                        RuntimeEvent(
                            type=EventType.PLAYBACK_START,
                            request_id=request_id,
                        )
                    )
                    self.event_bus.emit(
                        RuntimeEvent(
                            type=EventType.STATE_CHANGED,
                            request_id=request_id,
                            data={"state": "SPEAKING", "status": "SPEAKING"},
                        )
                    )
                    return
                if event_type == "audio_chunk":
                    audio = payload.pop("audio", b"")
                    sample_rate = int(payload.pop("sample_rate", 24_000))
                    await emit_event(
                        {
                            **payload,
                            "audio_base64": base64.b64encode(audio).decode("ascii"),
                            "audio_format": "wav",
                            "audio_sample_rate": sample_rate,
                        }
                    )
                    return
                await emit_event(payload)

            controller: (
                StreamingVoiceResponseController | VoiceResponseController
            ) = (
                StreamingVoiceResponseController(
                    synthesizer,
                    emit_voice_event,
                    cancellation=cancellation,
                    request_id=request_id,
                    max_concurrent_tts=self.settings.voice_tts_max_concurrent,
                    max_chars_per_chunk=self.settings.voice_tts_chunk_chars,
                    max_spoken_chars=spoken_char_budget,
                )
                if hasattr(synthesizer, "stream_audio")
                else VoiceResponseController(
                    synthesizer,
                    emit_voice_event,
                    cancellation=cancellation,
                    request_id=request_id,
                    max_concurrent_tts=self.settings.voice_tts_max_concurrent,
                    max_chars_per_chunk=self.settings.voice_tts_chunk_chars,
                    max_spoken_chars=spoken_char_budget,
                )
            )
            await controller.start()
            router_started_at = time.perf_counter()
            fast_match = self.fast_command_router.route(transcript)
            router_ms = round((time.perf_counter() - router_started_at) * 1_000, 3)
            if fast_match is not None:
                if fast_match.category == "cancel":
                    cancellation.cancel("Fast cancel command.")
                    cancellation.raise_if_cancelled()
                tool_ms: float | None = None
                if fast_match.tool:
                    self.event_bus.emit(
                        RuntimeEvent(
                            type=EventType.STATE_CHANGED,
                            request_id=request_id,
                            data={"state": "EXECUTING", "status": "EXECUTING"},
                        )
                    )
                    tool_started_at = time.perf_counter()
                    result = await self.toolbox.call(
                        fast_match.tool,
                        fast_match.arguments,
                        cancellation=cancellation,
                    )
                    tool_ms = round((time.perf_counter() - tool_started_at) * 1_000, 3)
                    reply = (
                        fast_match.reply
                        if bool(result.get("ok"))
                        else f"操作没有完成：{result.get('error') or '未知错误'}"
                    )
                else:
                    reply = fast_match.reply
                full_text = reply
                await emit_event({"type": "text_delta", "delta": reply})
                await controller.feed(reply)
                await controller.finish()
                total_ms = round((time.perf_counter() - turn_started_at) * 1_000, 3)
                fast_latency = {
                    "asr_ms": round((router_started_at - turn_started_at) * 1_000, 3),
                    "router_ms": router_ms,
                    "tool_ms": tool_ms,
                    "llm_ttft_ms": None,
                    "tts_ttfa_ms": (
                        round((tts_first_audio_at - tts_started_at) * 1_000, 3)
                        if tts_started_at is not None and tts_first_audio_at is not None
                        else None
                    ),
                    "first_audio_ms": (
                        round((tts_first_audio_at - turn_started_at) * 1_000, 3)
                        if tts_first_audio_at is not None
                        else None
                    ),
                    "total_ms": total_ms,
                    "audio_chunks": controller.audio_chunks,
                    "path": "fast",
                }
                self.event_bus.emit(
                    RuntimeEvent(
                        type=EventType.VOICE_LATENCY,
                        request_id=request_id,
                        data=fast_latency,
                    )
                )
                self._log_voice_latency(fast_latency)
                self.event_bus.emit(
                    RuntimeEvent(
                        type=EventType.STATE_CHANGED,
                        request_id=request_id,
                        data={"state": "COMPLETED", "status": "COMPLETE"},
                    )
                )
                await emit_event(
                    {
                        "type": "done",
                        "reply": full_text,
                        "tool_calls": 1 if fast_match.tool else 0,
                        "audio_chunks": controller.audio_chunks,
                        "latency": fast_latency,
                    },
                )
                return
            llm_started_at = time.perf_counter()
            self.event_bus.emit(
                RuntimeEvent(
                    type=EventType.LLM_START,
                    request_id=request_id,
                    data={"model": self.settings.voice_model},
                )
            )
            self.event_bus.emit(
                RuntimeEvent(
                    type=EventType.STATE_CHANGED,
                    request_id=request_id,
                    data={"state": "THINKING", "status": "THINKING"},
                )
            )
            tool_started_at: dict[str, float] = {}
            tool_ms_total = 0.0
            tool_calls_seen = 0

            def _on_tool_event(event: RuntimeEvent) -> None:
                nonlocal tool_ms_total, tool_calls_seen
                if event.task_id != request_id:
                    return
                event_type = str(event.type)
                if event_type == EventType.TOOL_START:
                    tool_calls_seen += 1
                    tool_started_at[
                        str(event.data.get("tool") or "")
                    ] = time.perf_counter()
                elif event_type in (EventType.TOOL_RESULT, EventType.TOOL_ERROR):
                    name = str(event.data.get("tool") or "")
                    started = tool_started_at.pop(name, None)
                    if started is not None:
                        tool_ms_total += (time.perf_counter() - started) * 1_000

            unsubscribe_tool_events = self.event_bus.subscribe(
                _on_tool_event,
                {EventType.TOOL_START, EventType.TOOL_RESULT, EventType.TOOL_ERROR},
            )
            try:
                async for delta in self.agent.ask_stream(
                    transcript,
                    session_id=payload.get("session_id"),
                    cancellation=cancellation,
                    task_id=request_id,
                ):
                    cancellation.raise_if_cancelled()
                    full_text += delta
                    if llm_first_token_at is None:
                        llm_first_token_at = time.perf_counter()
                        self.event_bus.emit(
                            RuntimeEvent(
                                type=EventType.LLM_FIRST_TOKEN,
                                request_id=request_id,
                                data={
                                    "ttft_ms": round(
                                        (llm_first_token_at - llm_started_at) * 1_000,
                                        3,
                                    )
                                },
                            )
                        )
                    await emit_event(
                        {"type": "text_delta", "delta": delta},
                    )
                    await controller.feed(delta)
            finally:
                unsubscribe_tool_events()

            await controller.finish()
            total_ms = round((time.perf_counter() - turn_started_at) * 1_000, 3)
            latency = {
                "asr_ms": round((router_started_at - turn_started_at) * 1_000, 3),
                "router_ms": router_ms,
                "tool_ms": round(tool_ms_total, 3) if tool_calls_seen else None,
                "llm_ttft_ms": (
                    round((llm_first_token_at - llm_started_at) * 1_000, 3)
                    if llm_first_token_at is not None
                    else None
                ),
                "tts_ttfa_ms": (
                    round((tts_first_audio_at - tts_started_at) * 1_000, 3)
                    if tts_started_at is not None and tts_first_audio_at is not None
                    else None
                ),
                "first_audio_ms": (
                    round((tts_first_audio_at - turn_started_at) * 1_000, 3)
                    if tts_first_audio_at is not None
                    else None
                ),
                "total_ms": total_ms,
                "audio_chunks": controller.audio_chunks,
                "path": "llm",
            }
            self.event_bus.emit(
                RuntimeEvent(
                    type=EventType.VOICE_LATENCY,
                    request_id=request_id,
                    data=latency,
                )
            )
            self._log_voice_latency(latency)
            self.event_bus.emit(
                RuntimeEvent(
                    type=EventType.STATE_CHANGED,
                    request_id=request_id,
                    data={"state": "COMPLETED", "status": "COMPLETE"},
                )
            )
            await emit_event(
                {
                    "type": "done",
                    "reply": full_text,
                    "tool_calls": tool_calls_seen,
                    "audio_chunks": controller.audio_chunks,
                    "latency": latency,
                },
            )
        except OperationCancelled:
            if controller is not None:
                await controller.abort(cancellation.reason or "User interrupted.")
            self.event_bus.emit(
                RuntimeEvent(
                    type=EventType.INTERRUPT,
                    request_id=request_id,
                    data={"reason": cancellation.reason or "User interrupted."},
                )
            )
            await emit_event(
                {
                    "type": "cancelled",
                    "reply": "已取消当前任务。",
                    "request_id": request_id,
                },
            )
        except Exception as exc:
            if controller is not None:
                await controller.abort(str(exc))
            self.event_bus.emit(
                RuntimeEvent(
                    type=EventType.STATE_CHANGED,
                    request_id=request_id,
                    data={"state": "ERROR", "status": str(exc)},
                )
            )
            await emit_event(
                {
                    "type": "error",
                    "request_id": request_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        finally:
            if controller is not None and not controller.finished:
                await controller.abort("Voice response stream closed.")
            self._cancellations.pop(request_id, None)
            if self._active_voice_request_id == request_id:
                self._active_voice_request_id = None
            try:
                await response.write_eof()
            except (ConnectionResetError, asyncio.CancelledError):
                pass
        return response

    def _get_speech_synthesizer(self) -> SpeechSynthesizer:
        if self._speech_synthesizer is None:
            self._speech_synthesizer = SpeechSynthesizer(self.settings)
        return self._speech_synthesizer

    async def _get_synthesizer(self) -> SpeechSynthesizer:
        if self._speech_synthesizer is not None:
            return self._speech_synthesizer
        async with self._synthesizer_lock:
            if self._speech_synthesizer is None:
                self._speech_synthesizer = await asyncio.to_thread(
                    self._get_speech_synthesizer
                )
        return self._speech_synthesizer

    async def handle_cancel(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            payload = {}

        request_id = str(payload.get("request_id") or "").strip()
        reason = str(payload.get("reason") or "User cancelled the task.")
        if request_id:
            tokens = [self._cancellations[request_id]] if request_id in self._cancellations else []
        else:
            tokens = list(self._cancellations.values())

        for token in tokens:
            token.cancel(reason)
        return web.json_response(
            {
                "ok": True,
                "cancelled": len(tokens),
                "request_id": request_id or None,
            }
        )

    async def handle_events(self, request: web.Request) -> web.StreamResponse:
        self._authorize_request(request)
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(request)
        queue: asyncio.Queue[RuntimeEvent] = asyncio.Queue()
        unsubscribe = self.event_bus.subscribe(EventBus.queue_handler(queue))
        try:
            for event in self.event_bus.history()[-32:]:
                if str(event.type) in {
                    "PERMISSION_REQUEST",
                    "PERMISSION_GRANTED",
                    "PERMISSION_DENIED",
                    "PERMISSION_RESOLVED",
                }:
                    continue
                await response.write(
                    f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n".encode()
                )
            for pending in await self.approval_broker.pending():
                event = RuntimeEvent(
                    type="PERMISSION_REQUEST",
                    data=pending.to_dict(),
                )
                await response.write(
                    f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n".encode()
                )
            while True:
                event = await queue.get()
                await response.write(
                    f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n".encode()
                )
        except asyncio.CancelledError:
            pass
        finally:
            unsubscribe()
        return response

    async def handle_asr_stream(self, request: web.Request) -> web.WebSocketResponse:
        self._authorize_request(request)
        owner_id, _ = self._request_scope(request)
        websocket = web.WebSocketResponse(heartbeat=20)
        await websocket.prepare(request)
        provider = create_asr_provider(
            self.settings,
            self.whisper,
            self.whisper_partial,
            initial_prompt=self._asr_initial_prompt(owner_id),
        )
        partial_task: asyncio.Task[None] | None = None
        last_partial_at = 0.0
        asr_started_at: float | None = None
        asr_first_emitted = False
        asr_turn_id: int | str | None = None

        async def send_partial() -> None:
            nonlocal asr_first_emitted
            event = await provider.partial()
            text = event.partial_text
            if text and not websocket.closed:
                if not asr_first_emitted and asr_started_at is not None:
                    asr_first_emitted = True
                    self.event_bus.emit(
                        RuntimeEvent(
                            type=EventType.ASR_FIRST,
                            data={
                                "elapsed_ms": round(
                                    (time.perf_counter() - asr_started_at) * 1_000,
                                    3,
                                )
                            },
                            request_id=str(asr_turn_id or ""),
                        )
                    )
                await websocket.send_json(
                    {
                        "type": "partial",
                        "text": text,
                        "turn_id": asr_turn_id,
                        "provider": event.provider,
                    }
                )

        try:
            async for message in websocket:
                if message.type == WSMsgType.BINARY:
                    if asr_started_at is None:
                        asr_started_at = time.perf_counter()
                        self.event_bus.emit(RuntimeEvent(type=EventType.MIC_START))
                        self.event_bus.emit(RuntimeEvent(type=EventType.ASR_START))
                    provider.push_audio(message.data)
                    now = time.monotonic()
                    if (
                        now - last_partial_at >= self.settings.asr_partial_interval_seconds
                        and (partial_task is None or partial_task.done())
                    ):
                        last_partial_at = now
                        partial_task = asyncio.create_task(send_partial())
                elif message.type == WSMsgType.TEXT:
                    payload = json.loads(message.data)
                    message_type = str(payload.get("type") or "")
                    if message_type == "start":
                        asr_turn_id = payload.get("turn_id")
                        await provider.start_session(
                            session_id=str(payload.get("session_id") or ""),
                            request_id=str(payload.get("request_id") or asr_turn_id or ""),
                        )
                        asr_started_at = time.perf_counter()
                        asr_first_emitted = False
                        self.event_bus.emit(RuntimeEvent(type=EventType.MIC_START))
                        self.event_bus.emit(RuntimeEvent(type=EventType.VAD_START))
                        self.event_bus.emit(
                            RuntimeEvent(
                                type=EventType.ASR_START,
                                data={"provider": provider.name},
                            )
                        )
                        await websocket.send_json(
                            {
                                "type": "ready",
                                "turn_id": asr_turn_id,
                                "provider": provider.name,
                            }
                        )
                    elif message_type == "stop":
                        if partial_task is not None:
                            await partial_task
                        event = await provider.flush()
                        text = event.final_text
                        if asr_started_at is not None:
                            self.event_bus.emit(
                                RuntimeEvent(
                                    type=EventType.ASR_FINAL,
                                    data={
                                        "elapsed_ms": round(
                                            (
                                                time.perf_counter()
                                                - asr_started_at
                                            )
                                            * 1_000,
                                            3,
                                        ),
                                        "characters": len(text),
                                    },
                                    request_id=str(asr_turn_id or ""),
                                )
                            )
                        await websocket.send_json(
                            {
                                "type": "final",
                                "text": text,
                                "turn_id": asr_turn_id,
                                "provider": event.provider,
                                "latency_ms": event.end_ms,
                            }
                        )
                        await provider.cancel()
                        asr_started_at = None
                        asr_first_emitted = False
                        asr_turn_id = None
                    elif message_type == "cancel":
                        await provider.cancel()
                        self.event_bus.emit(
                            RuntimeEvent(
                                type=EventType.INTERRUPT,
                                data={"source": "asr"},
                            )
                        )
                        asr_started_at = None
                        asr_first_emitted = False
                        await websocket.send_json(
                            {"type": "cancelled", "turn_id": asr_turn_id}
                        )
                        asr_turn_id = None
        finally:
            if partial_task is not None and not partial_task.done():
                partial_task.cancel()
            await provider.close()
            await websocket.close()
        return websocket

    async def handle_tasks(self, _request: web.Request) -> web.Response:
        self._authorize_request(_request)
        _owner_id, session_id = self._request_scope(_request)
        tasks = self.task_engine.list_tasks()
        if session_id:
            tasks = [task for task in tasks if task.session_id == session_id]
        return web.json_response(
            {"ok": True, "tasks": [task.to_dict() for task in tasks]}
        )

    async def handle_executions(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        _owner_id, session_id = self._request_scope(request)
        executions = self.execution_engine.store.list(
            max(1, min(int(request.query.get("limit", "50")), 200))
        )
        if session_id:
            executions = [
                execution
                for execution in executions
                if execution.session_id == session_id
            ]
        return web.json_response(
            {
                "ok": True,
                "executions": [execution.to_dict() for execution in executions],
            }
        )

    async def handle_execution_create(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        payload = await request.json()
        goal = str(payload.get("goal") or "").strip()
        if not goal:
            raise web.HTTPBadRequest(text="goal is required.")
        _owner_id, session_id = self._request_scope(request, payload)
        if not session_id:
            raise web.HTTPBadRequest(text="session_id is required.")
        execution_id = str(payload.get("execution_id") or uuid.uuid4().hex)
        plan: list[TaskStep] | None = None
        raw_plan = payload.get("plan")
        if isinstance(raw_plan, list):
            plan = [
                TaskStep(
                    id=str(item.get("id") or uuid.uuid4().hex),
                    title=str(item.get("title") or "Execution step"),
                    tool=str(item.get("tool") or ""),
                    metadata={
                        "arguments": dict(item.get("arguments") or {}),
                    },
                )
                for item in raw_plan
                if isinstance(item, dict)
            ]
        cancellation = CancellationToken()
        self._execution_cancellations[execution_id] = cancellation

        async def run_execution() -> None:
            try:
                await self.execution_engine.run(
                    goal,
                    session_id=session_id,
                    plan=plan,
                    task_id=payload.get("task_id"),
                    execution_id=execution_id,
                    cancellation=cancellation,
                )
            except Exception as exc:
                self.error_logger.log(
                    component="AGENT",
                    error=exc,
                    task_id=payload.get("task_id"),
                    recovery_action=(
                        "Execution engine applied retry/replan/abort policy."
                    ),
                )
            finally:
                self._execution_cancellations.pop(execution_id, None)
                self._execution_tasks.pop(execution_id, None)

        self._execution_tasks[execution_id] = asyncio.create_task(run_execution())
        return web.json_response(
            {"ok": True, "execution_id": execution_id, "status": "QUEUED"},
            status=202,
        )

    async def handle_execution_detail(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        _owner_id, session_id = self._request_scope(request)
        execution = self.execution_engine.store.get(
            str(request.match_info["execution_id"])
        )
        if execution is None:
            raise web.HTTPNotFound(text="Execution not found.")
        if session_id and execution.session_id != session_id:
            raise web.HTTPNotFound(text="Execution not found.")
        return web.json_response({"ok": True, "execution": execution.to_dict()})

    async def handle_execution_action(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        execution_id = str(request.match_info["execution_id"])
        action = str(request.match_info["action"])
        execution = self.execution_engine.store.get(execution_id)
        if execution is None:
            raise web.HTTPNotFound(text="Execution not found.")
        if action == "pause":
            result = await self.execution_engine.pause(execution_id)
        elif action == "cancel":
            self._execution_cancellations.get(execution_id, CancellationToken()).cancel(
                "User cancelled the execution."
            )
            result = await self.execution_engine.cancel(execution_id)
        elif action in {"resume", "continue"}:
            result = await self.execution_engine.resume(execution_id)
        else:
            raise web.HTTPBadRequest(text=f"Unsupported execution action: {action}")
        return web.json_response({"ok": True, "execution": result.to_dict()})

    async def handle_autonomous(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        payload = await request.json()
        goal = str(payload.get("goal") or "").strip()
        if not goal:
            raise web.HTTPBadRequest(text="goal is required.")
        _owner_id, session_id = self._request_scope(request, payload)
        if not session_id:
            raise web.HTTPBadRequest(text="session_id is required.")
        execution_id = str(payload.get("execution_id") or uuid.uuid4().hex)
        boundary_payload = dict(payload.get("boundary") or {})
        boundary = AutonomyBoundary(
            max_steps=max(1, min(int(boundary_payload.get("max_steps") or 20), 100)),
            timeout_seconds=max(
                1.0,
                float(boundary_payload.get("timeout_seconds") or 300),
            ),
            token_budget=max(
                1,
                int(boundary_payload.get("token_budget") or 120_000),
            ),
        )
        cancellation = CancellationToken()
        self._autonomous_cancellations[execution_id] = cancellation

        async def run_autonomous() -> None:
            try:
                await self.autonomous_loop.run(
                    goal,
                    session_id=session_id,
                    boundary=boundary,
                    cancellation=cancellation,
                    execution_id=execution_id,
                )
            except Exception as exc:
                self.error_logger.log(
                    component="AGENT",
                    error=exc,
                    recovery_action=(
                        "Autonomous loop stopped within its configured boundary."
                    ),
                )
            finally:
                self._autonomous_cancellations.pop(execution_id, None)
                self._autonomous_tasks.pop(execution_id, None)

        self._autonomous_tasks[execution_id] = asyncio.create_task(
            run_autonomous()
        )
        return web.json_response(
            {"ok": True, "execution_id": execution_id, "status": "QUEUED"},
            status=202,
        )

    async def handle_screen(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        force = request.query.get("force", "").lower() in {"1", "true", "yes"}
        state = await self.screen_observer.observe(force=force)
        return web.json_response({"ok": True, "screen": state.to_dict()})

    async def handle_multimodal(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        payload = await request.json()
        image = None
        if payload.get("image_base64"):
            image = base64.b64decode(str(payload["image_base64"]))
        file_paths = [
            Path(str(item)).expanduser()
            for item in payload.get("file_paths") or []
        ]
        results = await self.multimodal_engine.process(
            text=str(payload.get("text") or ""),
            image=image,
            file_paths=file_paths,
            include_screen=bool(payload.get("include_screen", False)),
        )
        return web.json_response(
            {"ok": True, "results": [result.to_dict() for result in results]}
        )

    async def handle_file_inspect(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        payload = await request.json()
        path = Path(str(payload.get("path") or "")).expanduser()
        if not path:
            raise web.HTTPBadRequest(text="path is required.")
        insight = await self.file_intelligence.inspect(path)
        return web.json_response({"ok": True, "file": insight.to_dict()})

    async def handle_file_search(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        payload = await request.json()
        root = Path(str(payload.get("path") or ".")).expanduser()
        query = str(payload.get("query") or "")
        limit = max(1, min(int(payload.get("limit") or 20), 100))
        insights = await self.file_intelligence.search(root, query, limit=limit)
        return web.json_response(
            {"ok": True, "files": [insight.to_dict() for insight in insights]}
        )

    async def handle_workflows(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        _owner_id, session_id = self._request_scope(request)
        workflows = self.workflow_engine.store.list(
            max(1, min(int(request.query.get("limit", "50")), 200))
        )
        if session_id:
            workflows = [
                workflow
                for workflow in workflows
                if workflow.session_id == session_id
            ]
        return web.json_response(
            {
                "ok": True,
                "workflows": [workflow.to_dict() for workflow in workflows],
            }
        )

    async def handle_workflow_create(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        payload = await request.json()
        _owner_id, session_id = self._request_scope(request, payload)
        if not session_id:
            raise web.HTTPBadRequest(text="session_id is required.")
        steps = [
            WorkflowStep(
                id=str(item.get("id") or uuid.uuid4().hex),
                title=str(item.get("title") or "Workflow step"),
                goal=str(item.get("goal") or item.get("title") or ""),
                tool=str(item.get("tool") or ""),
                arguments=dict(item.get("arguments") or {}),
                dependencies=[str(value) for value in item.get("dependencies") or []],
                rollback_tool=str(item.get("rollback_tool") or ""),
                rollback_arguments=dict(item.get("rollback_arguments") or {}),
                retry_policy=dict(item.get("retry_policy") or {}),
            )
            for item in payload.get("steps") or []
            if isinstance(item, dict)
        ]
        if not steps:
            raise web.HTTPBadRequest(text="steps are required.")
        workflow = await self.workflow_engine.create(
            name=str(payload.get("name") or "workflow"),
            session_id=session_id,
            steps=steps,
        )
        return web.json_response({"ok": True, "workflow": workflow.to_dict()})

    async def handle_workflow_detail(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        _owner_id, session_id = self._request_scope(request)
        workflow = self.workflow_engine.store.get(
            str(request.match_info["workflow_id"])
        )
        if workflow is None:
            raise web.HTTPNotFound(text="Workflow not found.")
        if session_id and workflow.session_id != session_id:
            raise web.HTTPNotFound(text="Workflow not found.")
        return web.json_response({"ok": True, "workflow": workflow.to_dict()})

    async def handle_workflow_action(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        workflow_id = str(request.match_info["workflow_id"])
        action = str(request.match_info["action"])
        workflow = self.workflow_engine.store.get(workflow_id)
        if workflow is None:
            raise web.HTTPNotFound(text="Workflow not found.")
        if action == "cancel":
            self._workflow_cancellations.get(
                workflow_id,
                CancellationToken(),
            ).cancel("User cancelled the workflow.")
            workflow.status = WorkflowStatus.CANCELLED
            self.workflow_store.save(workflow)
        elif action in {"run", "resume", "continue"}:
            cancellation = CancellationToken()
            self._workflow_cancellations[workflow_id] = cancellation

            async def run_workflow() -> None:
                try:
                    await self.workflow_engine.run(
                        workflow_id,
                        cancellation=cancellation,
                    )
                except Exception as exc:
                    self.error_logger.log(
                        component="WORKFLOW",
                        error=exc,
                        recovery_action=(
                            "Workflow rollback was attempted for completed steps."
                        ),
                    )
                finally:
                    self._workflow_cancellations.pop(workflow_id, None)
                    self._workflow_tasks.pop(workflow_id, None)

            self._workflow_tasks[workflow_id] = asyncio.create_task(run_workflow())
        else:
            raise web.HTTPBadRequest(text=f"Unsupported workflow action: {action}")
        current = self.workflow_engine.store.get(workflow_id)
        return web.json_response(
            {
                "ok": True,
                "workflow": current.to_dict() if current is not None else None,
            }
        )

    async def handle_trace(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        records = self.trace_store.list(
            task_id=request.query.get("task_id") or None,
            execution_id=request.query.get("execution_id") or None,
            limit=max(1, min(int(request.query.get("limit", "200")), 1_000)),
        )
        return web.json_response(
            {"ok": True, "trace": [record.to_dict() for record in records]}
        )

    async def handle_plugins(self, _request: web.Request) -> web.Response:
        self._authorize_request(_request)
        return web.json_response(
            {
                "ok": True,
                "plugins": [
                    {
                        "name": manifest.name,
                        "version": manifest.version,
                        "description": manifest.description,
                        "enabled": manifest.enabled,
                        "permissions": manifest.permissions,
                        "tools": [tool.get("name") for tool in manifest.tools],
                        "source": manifest.source,
                    }
                    for manifest in self.plugin_manager.manifests()
                ],
            }
        )

    async def handle_system_monitor(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        snapshot = await self.system_monitor.sample()
        return web.json_response(
            {"ok": True, "system": snapshot.to_dict()}
        )

    async def handle_system(self, _request: web.Request) -> web.Response:
        self._authorize_request(_request)
        return web.json_response({"ok": True, "system": await self.toolbox.get_system_info()})

    async def handle_pending_permissions(self, _request: web.Request) -> web.Response:
        self._authorize_request(_request)
        pending = await self.approval_broker.pending()
        return web.json_response(
            {"ok": True, "requests": [request.to_dict() for request in pending]}
        )

    async def handle_permission_resolve(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        request_id = str(request.match_info["request_id"])
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            payload = {}
        decision = str(payload.get("decision") or "")
        try:
            resolved = await self.approval_broker.resolve(request_id, decision)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if not resolved:
            raise web.HTTPNotFound(text="Permission request not found or already resolved.")
        return web.json_response({"ok": True, "request_id": request_id, "decision": decision})

    async def handle_tool(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="Invalid JSON.") from exc
        name = str(payload.get("name") or "").strip()
        arguments = payload.get("arguments")
        if not name or not isinstance(arguments, dict):
            raise web.HTTPBadRequest(text="name and object arguments are required.")
        result = await self.toolbox.call(name, arguments)
        return web.json_response(result)

    async def handle_schedule(self, _request: web.Request) -> web.Response:
        self._authorize_request(_request)
        return web.json_response(
            {"ok": True, "jobs": [job.to_dict() for job in self.scheduler.jobs()]}
        )

    async def handle_schedule_reminder(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="Invalid JSON.") from exc
        message = str(payload.get("message") or "").strip()
        if not message:
            raise web.HTTPBadRequest(text="message is required.")
        after_seconds = max(1.0, float(payload.get("after_seconds") or 0))
        reminder = self.reminder_service.schedule(
            message,
            after_seconds,
            str(payload.get("name") or "") or None,
        )
        return web.json_response({"ok": True, "reminder": reminder.to_dict()})

    async def handle_reminders(self, _request: web.Request) -> web.Response:
        self._authorize_request(_request)
        return web.json_response(
            {
                "ok": True,
                "reminders": [
                    reminder.to_dict() for reminder in self.reminder_service.list()
                ],
            }
        )

    async def handle_reminder_action(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        reminder_id = str(request.match_info["reminder_id"])
        action = str(request.match_info["action"])
        if action == "cancel":
            changed = self.reminder_service.cancel(reminder_id)
        elif action == "pause":
            changed = self.reminder_service.pause(reminder_id)
        elif action == "resume":
            changed = self.reminder_service.resume(reminder_id)
        else:
            raise web.HTTPBadRequest(text=f"Unsupported reminder action: {action}")
        if not changed:
            raise web.HTTPNotFound(text="Reminder not found.")
        return web.json_response(
            {"ok": True, "reminder_id": reminder_id, "action": action}
        )

    async def handle_schedule_action(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        job_id = str(request.match_info["job_id"])
        action = str(request.match_info["action"])
        if action == "cancel":
            changed = self.scheduler.cancel(job_id)
        elif action == "pause":
            changed = self.scheduler.pause(job_id)
        elif action == "resume":
            changed = self.scheduler.resume(job_id)
        else:
            raise web.HTTPBadRequest(text=f"Unsupported schedule action: {action}")
        if not changed:
            raise web.HTTPNotFound(text="Scheduled job not found.")
        return web.json_response({"ok": True, "job_id": job_id, "action": action})

    async def handle_task_detail(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        _owner_id, session_id = self._request_scope(request)
        task = self.task_engine.get_task(str(request.match_info["task_id"]))
        if task is None:
            raise web.HTTPNotFound(text="Task not found.")
        if session_id and task.session_id != session_id:
            raise web.HTTPNotFound(text="Task not found.")
        return web.json_response({"ok": True, "task": task.to_dict()})

    async def handle_task_action(self, request: web.Request) -> web.Response:
        self._authorize_request(request)
        _owner_id, session_id = self._request_scope(request)
        task_id = str(request.match_info["task_id"])
        action = str(request.match_info["action"])
        existing = self.task_engine.get_task(task_id)
        if existing is None:
            raise web.HTTPNotFound(text="Task not found.")
        if session_id and existing.session_id != session_id:
            raise web.HTTPNotFound(text="Task not found.")
        if action == "cancel":
            task = await self.task_engine.cancel_task(task_id)
        elif action == "resume":
            task = await self.task_engine.resume_task(task_id)
        elif action == "pause":
            task = await self.task_engine.pause_task(task_id)
        elif action == "retry":
            task = await self.task_engine.retry_task(task_id)
        elif action == "continue":
            reply = await self.agent.continue_task(task_id)
            task = self.task_engine.get_task(task_id)
            if task is None:
                raise web.HTTPNotFound(text="Task not found.")
            return web.json_response(
                {
                    "ok": True,
                    "task": task.to_dict(),
                    "reply": reply.text,
                    "tool_calls": reply.tool_calls,
                }
            )
        else:
            raise web.HTTPBadRequest(text=f"Unsupported task action: {action}")
        return web.json_response({"ok": True, "task": task.to_dict()})

    async def _transcribe_base64(self, audio_base64: str | None) -> str:
        if not audio_base64:
            raise web.HTTPBadRequest(text="audio_base64 or text is required.")
        raw = base64.b64decode(audio_base64)
        with tempfile.TemporaryDirectory(prefix="jarvis-bridge-") as temporary:
            root = Path(temporary)
            input_path = root / "input.webm"
            wav_path = root / "input.wav"
            input_path.write_bytes(raw)
            await asyncio.to_thread(self._decode_to_wav, input_path, wav_path)
            segments, _ = await asyncio.to_thread(
                self.whisper.transcribe,
                str(wav_path),
                language="zh",
                vad_filter=True,
                beam_size=self.settings.asr_final_beam_size,
                temperature=0.0,
                condition_on_previous_text=False,
                without_timestamps=True,
                initial_prompt=self.settings.whisper_initial_prompt,
                hotwords=self.settings.asr_hotwords,
                no_speech_threshold=self.settings.asr_no_speech_threshold,
            )
            text = normalize_transcript(
                "".join(segment.text for segment in segments)
            )
        if not text:
            raise web.HTTPUnprocessableEntity(text="Speech transcription was empty.")
        return text

    @staticmethod
    def _decode_to_wav(input_path: Path, output_path: Path) -> None:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-f",
                "wav",
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.decode(errors="ignore"))

    async def close(self) -> None:
        for cancellation in list(self._cancellations.values()):
            cancellation.cancel("Bridge shutting down.")
        self._cancellations.clear()
        for cancellation in list(self._execution_cancellations.values()):
            cancellation.cancel("Bridge shutting down.")
        for cancellation in list(self._workflow_cancellations.values()):
            cancellation.cancel("Bridge shutting down.")
        for cancellation in list(self._autonomous_cancellations.values()):
            cancellation.cancel("Bridge shutting down.")
        self._execution_cancellations.clear()
        if self._execution_tasks:
            await asyncio.gather(
                *self._execution_tasks.values(),
                return_exceptions=True,
            )
        self._execution_tasks.clear()
        if self._workflow_tasks:
            await asyncio.gather(
                *self._workflow_tasks.values(),
                return_exceptions=True,
            )
        self._workflow_tasks.clear()
        if self._autonomous_tasks:
            await asyncio.gather(
                *self._autonomous_tasks.values(),
                return_exceptions=True,
            )
        self._autonomous_tasks.clear()
        self._active_voice_request_id = None
        self._trace_unsubscribe()
        await self.system_monitor.stop()
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            self._keepalive_task = None
        self.memory.close()
        self.task_store.close()
        self.execution_store.close()
        self.workflow_store.close()
        self.trace_store.close()
        await self.browser_tool.close()
        await self.proactive_engine.stop()
        await self.mcp_manager.close()
        await self.scheduler.stop()
        self.reminder_store.close()
        self.production.shutdown(clean=True)


def create_app(settings: Settings | None = None) -> web.Application:
    settings = settings or Settings()
    bridge = LocalVoiceBridge(settings)
    app = web.Application(middlewares=[_cors_middleware])
    app["bridge"] = bridge
    app.router.add_get("/health", bridge.handle_health)
    app.router.add_get("/health/details", bridge.handle_health_details)
    app.router.add_get("/api/production", bridge.handle_production_snapshot)
    app.router.add_get("/api/production/config", bridge.handle_production_config)
    app.router.add_put("/api/production/config", bridge.handle_production_config)
    app.router.add_get("/api/production/permissions", bridge.handle_production_permissions)
    app.router.add_post("/api/production/permissions", bridge.handle_production_permissions)
    app.router.add_get("/api/production/logs", bridge.handle_production_logs)
    app.router.add_get("/api/production/data", bridge.handle_production_data)
    app.router.add_post("/api/production/data", bridge.handle_production_data)
    app.router.add_post(
        "/api/production/data/export",
        bridge.handle_production_data_export,
    )
    app.router.add_get(
        "/api/production/diagnostics",
        bridge.handle_production_diagnostics,
    )
    app.router.add_post(
        "/api/production/diagnostics",
        bridge.handle_production_diagnostics,
    )
    app.router.add_get("/api/production/backups", bridge.handle_production_backups)
    app.router.add_post("/api/production/backups", bridge.handle_production_backups)
    app.router.add_post(
        "/api/production/backups/restore",
        bridge.handle_production_restore,
    )
    app.router.add_get(
        "/api/production/recovery",
        bridge.handle_production_recovery,
    )
    app.router.add_post(
        "/api/production/recovery",
        bridge.handle_production_recovery,
    )
    app.router.add_get(
        "/api/production/services",
        bridge.handle_production_services,
    )
    app.router.add_post(
        "/api/production/services/{name}/{action}",
        bridge.handle_production_services,
    )
    app.router.add_get(
        "/api/production/launcher",
        bridge.handle_production_launcher,
    )
    app.router.add_post(
        "/api/production/launcher",
        bridge.handle_production_launcher,
    )
    app.router.add_get("/api/production/update", bridge.handle_production_update)
    app.router.add_post("/api/production/update", bridge.handle_production_update)
    app.router.add_get("/api/production/version", bridge.handle_production_version)
    app.router.add_post("/api/respond", bridge.handle_respond)
    app.router.add_post("/api/respond/stream", bridge.handle_respond_stream)
    app.router.add_get("/ws/asr", bridge.handle_asr_stream)
    app.router.add_post("/api/cancel", bridge.handle_cancel)
    app.router.add_get("/api/events", bridge.handle_events)
    app.router.add_get("/api/memory", bridge.handle_memory_list)
    app.router.add_post("/api/memory", bridge.handle_memory_create)
    app.router.add_put(
        "/api/memory/{memory_id}",
        bridge.handle_memory_update,
    )
    app.router.add_delete(
        "/api/memory/{memory_id}",
        bridge.handle_memory_delete,
    )
    app.router.add_get("/api/context", bridge.handle_context)
    app.router.add_get("/api/persona", bridge.handle_persona)
    app.router.add_put("/api/persona", bridge.handle_persona)
    app.router.add_get("/api/tasks", bridge.handle_tasks)
    app.router.add_get("/api/executions", bridge.handle_executions)
    app.router.add_post("/api/executions", bridge.handle_execution_create)
    app.router.add_get(
        "/api/executions/{execution_id}",
        bridge.handle_execution_detail,
    )
    app.router.add_post(
        "/api/executions/{execution_id}/{action}",
        bridge.handle_execution_action,
    )
    app.router.add_post("/api/autonomous", bridge.handle_autonomous)
    app.router.add_get("/api/screen", bridge.handle_screen)
    app.router.add_post("/api/multimodal", bridge.handle_multimodal)
    app.router.add_post("/api/files/inspect", bridge.handle_file_inspect)
    app.router.add_post("/api/files/search", bridge.handle_file_search)
    app.router.add_get("/api/workflows", bridge.handle_workflows)
    app.router.add_post("/api/workflows", bridge.handle_workflow_create)
    app.router.add_get(
        "/api/workflows/{workflow_id}",
        bridge.handle_workflow_detail,
    )
    app.router.add_post(
        "/api/workflows/{workflow_id}/{action}",
        bridge.handle_workflow_action,
    )
    app.router.add_get("/api/trace", bridge.handle_trace)
    app.router.add_get("/api/plugins", bridge.handle_plugins)
    app.router.add_get("/api/system/monitor", bridge.handle_system_monitor)
    app.router.add_post("/api/self-recovery", bridge.handle_self_recovery)
    app.router.add_get("/api/tasks/{task_id}", bridge.handle_task_detail)
    app.router.add_post("/api/tasks/{task_id}/{action}", bridge.handle_task_action)
    app.router.add_get("/api/system", bridge.handle_system)
    app.router.add_get("/api/permissions", bridge.handle_pending_permissions)
    app.router.add_post(
        "/api/permissions/{request_id}",
        bridge.handle_permission_resolve,
    )
    app.router.add_post("/api/tool", bridge.handle_tool)
    app.router.add_get("/api/schedule", bridge.handle_schedule)
    app.router.add_post("/api/schedule/reminder", bridge.handle_schedule_reminder)
    app.router.add_post("/api/schedule/{job_id}/{action}", bridge.handle_schedule_action)
    app.router.add_get("/api/reminders", bridge.handle_reminders)
    app.router.add_post(
        "/api/reminders/{reminder_id}/{action}",
        bridge.handle_reminder_action,
    )
    # 设备通道：ESP32 硬件终端 WebSocket 端点
    register_device_channel(app, bridge)
    app.on_startup.append(bridge.warmup)
    app.on_cleanup.append(lambda _app: bridge.close())
    return app


@web.middleware
async def _cors_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    if request.method == "OPTIONS":
        return web.Response(
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": (
                    "Content-Type, Authorization, X-Jarvis-Token, "
                    "X-Jarvis-Owner, X-Jarvis-Session"
                ),
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            }
        )
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, Authorization, X-Jarvis-Token, X-Jarvis-Owner, X-Jarvis-Session"
    )
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response


async def _write_stream_event(
    response: web.StreamResponse,
    payload: dict[str, Any],
) -> None:
    await response.write(
        (json.dumps(payload, ensure_ascii=False) + "\n").encode()
    )


def _allowed_config_path(path: str) -> bool:
    return any(
        path == prefix or path.startswith(f"{prefix}.")
        for prefix in (
            "llm",
            "asr",
            "tts",
            "logging",
            "performance",
            "network",
            "mcp",
            "updates",
            "ui",
            "permissions",
            "bridge",
            "plugins",
        )
    )


def _is_local_endpoint(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return hostname in {"127.0.0.1", "localhost", "::1"} or hostname.endswith(".local")


def _vision_cache_seconds(settings: Settings) -> int:
    if settings.performance_mode == "LOW_POWER":
        return max(15, settings.vision_frequency_seconds * 2)
    if settings.performance_mode == "HIGH_PERFORMANCE":
        return max(2, settings.vision_frequency_seconds // 2)
    return settings.vision_frequency_seconds


def _memory_payload(memory: Any) -> dict[str, Any]:
    return {
        "id": memory.id,
        "content": memory.content,
        "category": memory.category,
        "importance": memory.importance,
        "confidence": memory.confidence,
        "summary": memory.summary,
        "metadata": memory.metadata or {},
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
        "last_used_at": memory.last_used_at,
        "source": memory.source,
        "task_id": memory.task_id,
        "session_id": memory.session_id,
        "owner_id": memory.owner_id,
    }


def run_bridge(settings: Settings, host: str = "127.0.0.1", port: int = 8765) -> None:
    print(f"Jarvis local voice bridge: http://{host}:{port}")
    web.run_app(create_app(settings), host=host, port=port, print=None)
