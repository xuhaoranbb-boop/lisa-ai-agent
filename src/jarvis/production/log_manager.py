from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis.runtime.logging_utils import redact_text

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class RotatingJSONLHandler(logging.Handler):
    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
    ) -> None:
        super().__init__()
        self.path = path
        self.max_bytes = max(1024, int(max_bytes))
        self.backup_count = max(1, int(backup_count))
        self._lock = threading.RLock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8") as file:
                    file.write(json.dumps(self._payload(record), ensure_ascii=False) + "\n")
        except Exception:
            self.handleError(record)

    def _rotate_if_needed(self) -> None:
        if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
            return
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            destination = self.path.with_name(f"{self.path.name}.{index + 1}")
            if source.exists():
                os.replace(source, destination)
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))

    @staticmethod
    def _payload(record: logging.LogRecord) -> dict[str, Any]:
        context = getattr(record, "jarvis_context", {})
        if not isinstance(context, dict):
            context = {"value": str(context)}
        return {
            "timestamp": datetime.fromtimestamp(
                record.created,
                UTC,
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "module": record.name,
            "message": redact_text(record.getMessage()),
            "trace_id": context.get("trace_id"),
            "task_id": context.get("task_id"),
            "owner_id": context.get("owner_id"),
            "metadata": {
                key: redact_text(value)
                for key, value in context.items()
                if key not in {"trace_id", "task_id", "owner_id"}
            },
        }


class LogManager:
    def __init__(
        self,
        log_dir: Path,
        *,
        level: str = "INFO",
        max_bytes: int = 10 * 1024 * 1024,
        retention_days: int = 14,
    ) -> None:
        self.log_dir = log_dir
        self.level = level.upper()
        self.retention_days = max(1, int(retention_days))
        self.handler = RotatingJSONLHandler(
            log_dir / "jarvis.jsonl",
            max_bytes=max_bytes,
            backup_count=max(1, min(30, self.retention_days)),
        )
        self.handler.setLevel(_LEVELS.get(self.level, logging.INFO))
        self.logger = logging.getLogger("jarvis.production")
        self.logger.setLevel(_LEVELS.get(self.level, logging.INFO))
        self.logger.propagate = False
        if self.handler not in self.logger.handlers:
            self.logger.handlers = [self.handler]
        self.cleanup()
        self.log(
            "INFO",
            "logging",
            "Log manager initialized.",
            metadata={"retention_days": self.retention_days},
        )

    def log(
        self,
        level: str,
        module: str,
        message: str,
        *,
        trace_id: str | None = None,
        task_id: str | None = None,
        owner_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        normalized = level.upper()
        if normalized == "WARN":
            normalized = "WARNING"
        numeric_level = _LEVELS.get(normalized, logging.INFO)
        context = {
            "trace_id": trace_id,
            "task_id": task_id,
            "owner_id": owner_id,
            **(metadata or {}),
        }
        self.logger.log(
            numeric_level,
            message,
            extra={"jarvis_context": context},
        )

    def query(
        self,
        *,
        level: str | None = None,
        module: str | None = None,
        trace_id: str | None = None,
        task_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        minimum = _LEVELS.get((level or "").upper(), 0)
        for path in self._files_newest_first():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if minimum and _LEVELS.get(str(payload.get("level")), 0) < minimum:
                    continue
                if module and module.lower() not in str(payload.get("module", "")).lower():
                    continue
                if trace_id and payload.get("trace_id") != trace_id:
                    continue
                if task_id and payload.get("task_id") != task_id:
                    continue
                records.append(payload)
                if len(records) >= max(1, min(limit, 2_000)):
                    return records
        return records

    def cleanup(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        removed = 0
        if not self.log_dir.exists():
            return removed
        for path in self.log_dir.glob("jarvis.jsonl*"):
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            except OSError:
                continue
            if modified < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def stats(self) -> dict[str, Any]:
        files = list(self.log_dir.glob("jarvis.jsonl*"))
        return {
            "directory": str(self.log_dir),
            "files": len(files),
            "size_bytes": sum(path.stat().st_size for path in files if path.exists()),
            "level": self.level,
            "retention_days": self.retention_days,
        }

    def _files_newest_first(self) -> list[Path]:
        return sorted(
            self.log_dir.glob("jarvis.jsonl*"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
