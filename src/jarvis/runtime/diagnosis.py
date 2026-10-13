from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from jarvis.runtime.events import EventBus, RuntimeEvent
from jarvis.runtime.health import HealthReport, HealthStatus
from jarvis.runtime.trace import TraceRecord


class DiagnosisSeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class Diagnosis:
    component: str
    problem: str
    severity: DiagnosisSeverity
    likely_cause: str
    suggested_action: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "problem": self.problem,
            "severity": str(self.severity),
            "likely_cause": self.likely_cause,
            "suggested_action": self.suggested_action,
            "evidence": self.evidence,
        }


class DiagnosisEngine:
    def __init__(
        self,
        *,
        llm_error_rate_threshold: float = 0.2,
        tool_error_rate_threshold: float = 0.25,
        latency_warning_ms: float = 5_000.0,
    ) -> None:
        self.llm_error_rate_threshold = llm_error_rate_threshold
        self.tool_error_rate_threshold = tool_error_rate_threshold
        self.latency_warning_ms = latency_warning_ms

    def diagnose(
        self,
        health: HealthReport,
        trace: list[TraceRecord] | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> list[Diagnosis]:
        diagnoses: list[Diagnosis] = []
        for component in health.components:
            if component.status == HealthStatus.HEALTHY:
                continue
            severity = {
                HealthStatus.DEGRADED: DiagnosisSeverity.MEDIUM,
                HealthStatus.ERROR: DiagnosisSeverity.HIGH,
                HealthStatus.OFFLINE: DiagnosisSeverity.CRITICAL,
            }[component.status]
            diagnoses.append(
                Diagnosis(
                    component=component.name,
                    problem=f"{component.name} is {component.status}.",
                    severity=severity,
                    likely_cause=component.message or "Component health check failed.",
                    suggested_action=_suggest_action(component.name),
                    evidence=component.to_dict(),
                )
            )

        records = trace or []
        llm_records = [
            record
            for record in records
            if "LLM" in record.event_type.upper()
        ]
        llm_errors = [record for record in llm_records if record.error]
        if llm_records:
            rate = len(llm_errors) / len(llm_records)
            if rate >= self.llm_error_rate_threshold:
                diagnoses.append(
                    Diagnosis(
                        component="llm",
                        problem=f"LLM error rate is {rate:.0%}.",
                        severity=DiagnosisSeverity.HIGH,
                        likely_cause="Provider errors, throttling, or unstable network.",
                        suggested_action="Use model fallback and inspect provider errors.",
                        evidence={"error_rate": rate, "samples": len(llm_records)},
                    )
                )

        tool_records = [
            record
            for record in records
            if record.tool or "TOOL" in record.event_type.upper()
        ]
        tool_errors = [record for record in tool_records if record.error]
        if tool_records:
            rate = len(tool_errors) / len(tool_records)
            if rate >= self.tool_error_rate_threshold:
                diagnoses.append(
                    Diagnosis(
                        component="tools",
                        problem=f"Tool failure rate is {rate:.0%}.",
                        severity=DiagnosisSeverity.MEDIUM,
                        likely_cause="One or more tools are failing repeatedly.",
                        suggested_action="Inspect recent tool errors and disable unstable tools.",
                        evidence={"error_rate": rate, "samples": len(tool_records)},
                    )
                )

        slow = [
            record
            for record in records
            if record.latency_ms >= self.latency_warning_ms
        ]
        if slow:
            diagnoses.append(
                Diagnosis(
                    component=_component_for_trace(slow[0]),
                    problem=f"{len(slow)} operation(s) exceeded {self.latency_warning_ms:.0f} ms.",
                    severity=DiagnosisSeverity.MEDIUM,
                    likely_cause="Slow model, vision, disk, or tool operation.",
                    suggested_action=(
                        "Compact context, reduce vision frequency, or use a faster model."
                    ),
                    evidence={
                        "max_latency_ms": max(record.latency_ms for record in slow)
                    },
                )
            )
        for key, threshold, component in (
            ("memory_percent", 90.0, "memory"),
            ("disk_free_gb", 5.0, "disk"),
        ):
            value = (metrics or {}).get(key)
            if value is None:
                continue
            if key == "disk_free_gb" and float(value) <= threshold:
                diagnoses.append(
                    Diagnosis(
                        component=component,
                        problem=f"{key} is {value}.",
                        severity=DiagnosisSeverity.HIGH,
                        likely_cause="Low system resources.",
                        suggested_action="Free disk space before continuing long tasks.",
                        evidence={key: value},
                    )
                )
            elif key == "memory_percent" and float(value) >= threshold:
                diagnoses.append(
                    Diagnosis(
                        component=component,
                        problem=f"{key} is {value}.",
                        severity=DiagnosisSeverity.MEDIUM,
                        likely_cause="High memory usage.",
                        suggested_action="Close unnecessary applications or reduce context.",
                        evidence={key: value},
                    )
                )
        return diagnoses


RecoveryAction = Callable[[Diagnosis], Awaitable[dict[str, Any]]]


class SelfRecoveryEngine:
    """Run only registered low-risk recovery actions."""

    def __init__(self, event_bus: EventBus | None = None) -> None:
        self.event_bus = event_bus or EventBus()
        self._actions: dict[str, RecoveryAction] = {}
        self._history: list[dict[str, Any]] = []

    def register(self, component: str, action: RecoveryAction) -> None:
        self._actions[component] = action

    async def recover(self, diagnosis: Diagnosis) -> dict[str, Any]:
        action = self._actions.get(diagnosis.component)
        result: dict[str, Any] = {
            "component": diagnosis.component,
            "severity": str(diagnosis.severity),
            "attempted": action is not None,
            "success": False,
            "message": "",
        }
        if action is None:
            result["message"] = "No safe automatic recovery action is registered."
        else:
            try:
                details = await action(diagnosis)
                result.update({"success": True, "details": details})
            except Exception as exc:
                result.update(
                    {
                        "success": False,
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )
        self._history.append(result)
        await self.event_bus.emit_async(
            RuntimeEvent(
                type="SELF_RECOVERY",
                data={
                    "diagnosis": diagnosis.to_dict(),
                    "result": result,
                },
            )
        )
        return result

    def history(self) -> list[dict[str, Any]]:
        return list(self._history)


def _suggest_action(component: str) -> str:
    return {
        "mcp": "Reconnect MCP and refresh tool discovery.",
        "llm": "Try the configured fallback model.",
        "database": "Check SQLite locks, permissions, and free disk space.",
        "memory": "Run memory maintenance or reduce retrieval size.",
        "desktop": "Check desktop permissions and window access.",
        "vision": "Check vision model configuration and screenshot access.",
        "browser": "Restart the browser adapter or verify Playwright/Edge.",
        "voice": "Reconnect ASR/TTS and check audio devices.",
    }.get(component, "Inspect component logs and retry once.")


def _component_for_trace(record: TraceRecord) -> str:
    event = record.event_type.upper()
    if "TTS" in event:
        return "tts"
    if "ASR" in event:
        return "asr"
    if "LLM" in event:
        return "llm"
    if record.tool:
        return "tools"
    return "agent"
