import json
import time
from types import SimpleNamespace

import pytest

from jarvis.runtime.cache import TTLCache
from jarvis.runtime.diagnosis import (
    Diagnosis,
    DiagnosisEngine,
    DiagnosisSeverity,
    SelfRecoveryEngine,
)
from jarvis.runtime.events import EventBus
from jarvis.runtime.health import (
    ComponentHealth,
    HealthChecker,
    HealthReport,
    HealthStatus,
)
from jarvis.runtime.logging_utils import ErrorLogger, redact_text
from jarvis.runtime.trace import AgentTraceStore, TraceRecord


class FakeTasks:
    def list_tasks(self, limit: int = 100):  # type: ignore[no-untyped-def]
        return []


class FakeMemory:
    def list_memories(self, limit: int = 500):  # type: ignore[no-untyped-def]
        return []


@pytest.mark.asyncio
async def test_health_checker_reports_component_status() -> None:
    bridge = SimpleNamespace(
        memory=FakeMemory(),
        task_engine=FakeTasks(),
        execution_engine=SimpleNamespace(store=SimpleNamespace(list=lambda limit: [])),
        router=SimpleNamespace(provider_for=lambda role: object()),
        mcp_manager=SimpleNamespace(_configs={}, _clients={}),
        screen_observer=SimpleNamespace(provider=None),
        browser_tool=object(),
        settings=SimpleNamespace(browser_backend="web"),
        toolbox=SimpleNamespace(
            desktop=SimpleNamespace(
                observe=lambda include_screenshot=False: _observe()
            )
        ),
        whisper=object(),
        _speech_synthesizer=object(),
    )

    async def _observe():  # type: ignore[no-untyped-def]
        return SimpleNamespace(windows=[])

    report = await HealthChecker(bridge).check()
    statuses = {component.name: component.status for component in report.components}
    assert statuses["database"] == HealthStatus.HEALTHY
    assert statuses["vision"] == HealthStatus.DEGRADED
    assert report.status == HealthStatus.DEGRADED


def test_diagnosis_engine_detects_slow_and_erroring_components() -> None:
    report = HealthReport(
        status=HealthStatus.ERROR,
        components=[
            ComponentHealth(
                name="mcp",
                status=HealthStatus.OFFLINE,
                message="MCP disconnected",
            )
        ],
        checked_at=time.time(),
    )
    trace = [
        TraceRecord(
            id=1,
            task_id="task",
            execution_id="exec",
            turn_id="turn",
            event_type="LLM_FIRST_TOKEN",
            model="deepseek-chat",
            tool="",
            latency_ms=8_000,
            input_tokens=1,
            output_tokens=1,
            estimated_cost=0,
            error="",
        ),
        TraceRecord(
            id=2,
            task_id="task",
            execution_id="exec",
            turn_id="turn",
            event_type="TOOL_ERROR",
            model="",
            tool="file_read",
            latency_ms=100,
            input_tokens=0,
            output_tokens=0,
            estimated_cost=0,
            error="failed",
        ),
    ]
    diagnoses = DiagnosisEngine().diagnose(report, trace=trace)

    assert any(item.component == "mcp" for item in diagnoses)
    assert any(
        item.severity in {DiagnosisSeverity.HIGH, DiagnosisSeverity.CRITICAL}
        for item in diagnoses
    )
    assert any("exceeded" in item.problem for item in diagnoses)


@pytest.mark.asyncio
async def test_self_recovery_runs_only_registered_low_risk_actions() -> None:
    bus = EventBus()
    engine = SelfRecoveryEngine(bus)
    called: list[str] = []

    async def recover_mcp(_diagnosis: Diagnosis) -> dict[str, object]:
        called.append("mcp")
        return {"reconnected": True}

    engine.register("mcp", recover_mcp)
    result = await engine.recover(
        Diagnosis(
            component="mcp",
            problem="offline",
            severity=DiagnosisSeverity.HIGH,
            likely_cause="disconnect",
            suggested_action="reconnect",
        )
    )
    missing = await engine.recover(
        Diagnosis(
            component="database",
            problem="offline",
            severity=DiagnosisSeverity.CRITICAL,
            likely_cause="lock",
            suggested_action="manual",
        )
    )

    assert result["success"] is True
    assert called == ["mcp"]
    assert missing["success"] is False
    assert any(str(event.type) == "SELF_RECOVERY" for event in bus.history())


def test_ttl_cache_expires_and_invalidates() -> None:
    cache: TTLCache[str, str] = TTLCache(ttl_seconds=0.1, max_entries=2)
    cache.set("a", "one")
    assert cache.get("a") == "one"
    assert cache.invalidate("a") is True
    assert cache.get("a") is None
    cache.set("b", "two", ttl_seconds=0.01)
    time.sleep(0.02)
    assert cache.get("b") is None


def test_error_logger_redacts_credentials(tmp_path) -> None:  # type: ignore[no-untyped-def]
    logger = ErrorLogger(tmp_path / "errors.jsonl")
    record = logger.log(
        component="llm",
        error="Authorization: Bearer secret-token-value",
        metadata={"api_key": "sk-1234567890abcdef"},
    )
    assert "secret-token-value" not in record.message
    assert "sk-" not in record.metadata["api_key"]
    assert redact_text("password=abc123") == "[REDACTED]"


def test_agent_trace_redacts_secret_values(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = AgentTraceStore(tmp_path / "trace.db")
    try:
        store.record(
            TraceRecord(
                id=0,
                task_id="task",
                execution_id="exec",
                turn_id="turn",
                event_type="TOOL_REQUEST",
                model="",
                tool="payment",
                latency_ms=1,
                input_tokens=0,
                output_tokens=0,
                estimated_cost=0,
                error="",
                data={"arguments": {"token": "Bearer secret-token-value"}},
            )
        )
        record = store.list(task_id="task")[0]
        assert "secret-token-value" not in json.dumps(record.data)
    finally:
        store.close()
