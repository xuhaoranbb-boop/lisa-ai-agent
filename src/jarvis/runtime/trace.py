from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from jarvis.runtime.events import EventBus, RuntimeEvent
from jarvis.runtime.logging_utils import redact_text


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class TraceRecord:
    id: int
    task_id: str | None
    execution_id: str | None
    turn_id: str | None
    event_type: str
    model: str
    tool: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    estimated_cost: float
    error: str
    data: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "turn_id": self.turn_id,
            "event_type": self.event_type,
            "model": self.model,
            "tool": self.tool,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost": self.estimated_cost,
            "error": self.error,
            "data": self.data,
            "created_at": self.created_at,
        }


class AgentTraceStore:
    def __init__(self, path: Path, *, max_records: int = 10_000) -> None:
        self.path = path
        self.max_records = max(100, max_records)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS agent_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT,
                    execution_id TEXT,
                    turn_id TEXT,
                    event_type TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    tool TEXT NOT NULL DEFAULT '',
                    latency_ms REAL NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_traces_task
                    ON agent_traces(task_id, id);
                CREATE INDEX IF NOT EXISTS idx_agent_traces_execution
                    ON agent_traces(execution_id, id);
                """
            )

    def record(self, record: TraceRecord) -> TraceRecord:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO agent_traces(
                    task_id, execution_id, turn_id, event_type, model, tool,
                    latency_ms, input_tokens, output_tokens, estimated_cost,
                    error, data_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.task_id,
                    record.execution_id,
                    record.turn_id,
                    record.event_type,
                    record.model,
                    record.tool,
                    record.latency_ms,
                    record.input_tokens,
                    record.output_tokens,
                    record.estimated_cost,
                    record.error,
                    json.dumps(
                        _redact_data(record.data),
                        ensure_ascii=False,
                        default=str,
                    ),
                    record.created_at,
                ),
            )
            record_id = int(cursor.lastrowid)
            if record_id % 100 == 0:
                self._connection.execute(
                    """
                    DELETE FROM agent_traces
                    WHERE id NOT IN (
                        SELECT id FROM agent_traces ORDER BY id DESC LIMIT ?
                    )
                    """,
                    (self.max_records,),
                )
        return TraceRecord(
            **{
                **record.__dict__,
                "id": record_id,
            }
        )

    def list(
        self,
        *,
        task_id: str | None = None,
        execution_id: str | None = None,
        limit: int = 200,
    ) -> list[TraceRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if execution_id:
            clauses.append("execution_id = ?")
            params.append(execution_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT *
                FROM agent_traces
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> TraceRecord:
        return TraceRecord(
            id=int(row["id"]),
            task_id=str(row["task_id"]) if row["task_id"] is not None else None,
            execution_id=(
                str(row["execution_id"])
                if row["execution_id"] is not None
                else None
            ),
            turn_id=str(row["turn_id"]) if row["turn_id"] is not None else None,
            event_type=str(row["event_type"]),
            model=str(row["model"]),
            tool=str(row["tool"]),
            latency_ms=float(row["latency_ms"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            estimated_cost=float(row["estimated_cost"]),
            error=str(row["error"]),
            data=dict(json.loads(str(row["data_json"] or "{}"))),
            created_at=str(row["created_at"]),
        )


def _redact_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_data(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_data(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class AgentTrace:
    def __init__(self, store: AgentTraceStore) -> None:
        self.store = store
        self.started_at = time.perf_counter()

    def attach(self, event_bus: EventBus) -> Callable[[], None]:
        def handler(event: RuntimeEvent) -> None:
            data = event.data
            self.store.record(
                TraceRecord(
                    id=0,
                    task_id=event.task_id,
                    execution_id=str(data.get("execution_id") or "") or None,
                    turn_id=event.request_id,
                    event_type=str(event.type),
                    model=str(data.get("model") or ""),
                    tool=str(data.get("tool") or ""),
                    latency_ms=float(data.get("elapsed_ms") or 0),
                    input_tokens=int(data.get("input_tokens") or 0),
                    output_tokens=int(data.get("output_tokens") or 0),
                    estimated_cost=float(data.get("estimated_cost") or 0),
                    error=str(data.get("error") or ""),
                    data=data,
                )
            )

        return event_bus.subscribe(handler)
