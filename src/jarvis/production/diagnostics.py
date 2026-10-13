from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis.production.environment import RuntimePaths
from jarvis.production.service_manager import ServiceManager
from jarvis.runtime.health import HealthChecker
from jarvis.runtime.logging_utils import redact_text


@dataclass
class DiagnosticReport:
    generated_at: str
    version: str
    environment: str
    status: str
    checks: dict[str, Any] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "version": self.version,
            "environment": self.environment,
            "status": self.status,
            "checks": self.checks,
            "findings": self.findings,
        }


class DiagnosticsCenter:
    def __init__(
        self,
        *,
        paths: RuntimePaths,
        health_checker: HealthChecker,
        service_manager: ServiceManager,
        version: str,
    ) -> None:
        self.paths = paths
        self.health_checker = health_checker
        self.service_manager = service_manager
        self.version = version

    async def collect(self) -> DiagnosticReport:
        health = await self.health_checker.check()
        services = await self.service_manager.check_all(auto_restart=False)
        checks = {
            "health": health.to_dict(),
            "services": services,
            "database": self._database_check(),
            "disk": self._disk_check(),
            "runtime": self.paths.to_dict(),
            "environment_variables": self._safe_environment(),
        }
        findings: list[str] = []
        for component in health.components:
            if str(component.status) != "HEALTHY":
                findings.append(f"{component.name}: {component.status} {component.message}".strip())
        for service in services:
            if service["state"] in {"FAILED", "DEGRADED"}:
                findings.append(
                    f"service {service['name']}: {service['state']} {service['last_error']}".strip()
                )
        status = "HEALTHY" if not findings else "NEEDS_ATTENTION"
        return DiagnosticReport(
            generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            version=self.version,
            environment=str(self.paths.environment),
            status=status,
            checks=checks,
            findings=findings,
        )

    def export(
        self,
        report: DiagnosticReport,
        *,
        path: Path | None = None,
    ) -> Path:
        destination = path or self.paths.exports / (
            f"jarvis-diagnostics-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(_redact_value(report.to_dict()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return destination

    def _database_check(self) -> dict[str, Any]:
        if not self.paths.database.exists():
            return {"ok": False, "message": "Database file does not exist."}
        try:
            with sqlite3.connect(self.paths.database) as connection:
                integrity = connection.execute("PRAGMA quick_check").fetchone()
                journal = connection.execute("PRAGMA journal_mode").fetchone()
            return {
                "ok": bool(integrity and integrity[0] == "ok"),
                "integrity": str(integrity[0] if integrity else "unknown"),
                "journal_mode": str(journal[0] if journal else "unknown"),
                "size_bytes": self.paths.database.stat().st_size,
            }
        except Exception as exc:
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    def _disk_check(self) -> dict[str, Any]:
        usage = shutil.disk_usage(self.paths.root)
        return {
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "used_percent": round(
                ((usage.total - usage.free) / max(usage.total, 1)) * 100,
                2,
            ),
        }

    @staticmethod
    def _safe_environment() -> dict[str, Any]:
        allowed = {
            "JARVIS_ENVIRONMENT",
            "JARVIS_MODEL",
            "JARVIS_MODEL_BASE_URL",
            "JARVIS_VOICE_MODEL",
            "JARVIS_WHISPER_DEVICE",
            "JARVIS_BROWSER_BACKEND",
        }
        return {
            key: redact_text(value)
            for key, value in os.environ.items()
            if key in allowed
        }


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value
