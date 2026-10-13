from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any


@dataclass
class RecoveryReport:
    unclean_shutdown: bool = False
    previous_started_at: str = ""
    previous_clean_shutdown: bool = True
    unfinished_tasks: list[dict[str, Any]] = field(default_factory=list)
    components: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unclean_shutdown": self.unclean_shutdown,
            "previous_started_at": self.previous_started_at,
            "previous_clean_shutdown": self.previous_clean_shutdown,
            "unfinished_tasks": self.unfinished_tasks,
            "components": self.components,
            "requires_user_decision": bool(
                self.unclean_shutdown and self.unfinished_tasks
            ),
        }


class CrashRecovery:
    """Track component heartbeats and detect abnormal application exits."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self._lock = RLock()
        self.last_report = RecoveryReport()

    def begin_startup(self, components: list[str]) -> RecoveryReport:
        with self._lock:
            previous = self._read()
            previous_clean = bool(previous.get("clean_shutdown", True))
            report = RecoveryReport(
                unclean_shutdown=bool(previous) and not previous_clean,
                previous_started_at=str(previous.get("started_at") or ""),
                previous_clean_shutdown=previous_clean,
                components={name: "STARTING" for name in components},
            )
            self.last_report = report
            self._write(
                {
                    "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "clean_shutdown": False,
                    "components": report.components,
                }
            )
            return report

    def attach_unfinished_tasks(self, tasks: list[dict[str, Any]]) -> RecoveryReport:
        self.last_report.unfinished_tasks = tasks
        self.heartbeat("tasks", "RECOVERY_NEEDED" if tasks else "READY")
        return self.last_report

    def heartbeat(self, component: str, state: str = "READY") -> None:
        with self._lock:
            payload = self._read()
            if not payload:
                return
            components = dict(payload.get("components") or {})
            components[component] = state
            payload["components"] = components
            payload["heartbeat_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            self.last_report.components = components
            self._write(payload)

    def mark_shutdown(self, *, clean: bool = True) -> None:
        if not clean:
            return
        with self._lock:
            payload = self._read()
            if not payload:
                return
            payload["clean_shutdown"] = True
            payload["stopped_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            payload["components"] = {
                name: "STOPPED"
                for name in dict(payload.get("components") or {})
            }
            self._write(payload)

    def report(self) -> RecoveryReport:
        return self.last_report

    def _read(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {"clean_shutdown": False}

    def _write(self, payload: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)
