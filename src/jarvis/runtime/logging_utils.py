from __future__ import annotations

import json
import re
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I),
    re.compile(
        r"\b(api[_-]?key|token|password|passwd|pwd|authorization|cookie)"
        r"\s*[:=]\s*[^\s,;]+",
        re.I,
    ),
)


def redact_text(value: object) -> str:
    text = str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


@dataclass(frozen=True)
class ErrorLog:
    component: str
    error_type: str
    message: str
    timestamp: str
    trace_id: str | None = None
    task_id: str | None = None
    stack: str = ""
    recovery_action: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            "task_id": self.task_id,
            "component": self.component,
            "error_type": self.error_type,
            "message": self.message,
            "stack": self.stack,
            "recovery_action": self.recovery_action,
            "metadata": self.metadata,
        }


class ErrorLogger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.records: list[ErrorLog] = []

    def log(
        self,
        *,
        component: str,
        error: BaseException | str,
        trace_id: str | None = None,
        task_id: str | None = None,
        recovery_action: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ErrorLog:
        error_type = type(error).__name__ if isinstance(error, BaseException) else "Error"
        message = redact_text(error)
        stack = (
            redact_text("".join(traceback.format_exception(error)))
            if isinstance(error, BaseException)
            else ""
        )
        record = ErrorLog(
            component=component.upper(),
            error_type=error_type,
            message=message,
            timestamp=datetime.now(UTC).isoformat(timespec="milliseconds"),
            trace_id=trace_id,
            task_id=task_id,
            stack=stack,
            recovery_action=recovery_action,
            metadata=_redact_mapping(metadata or {}),
        )
        self.records.append(record)
        self.records = self.records[-2_000:]
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        return record


def _redact_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): redact_text(item) if isinstance(item, str) else item
        for key, item in value.items()
    }
