from __future__ import annotations

import asyncio
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from threading import RLock

from jarvis.runtime.events import EventBus, EventType, RuntimeEvent
from jarvis.runtime.scheduler import ScheduledJob, TaskScheduler


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class ReminderStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True)
class Reminder:
    id: str
    name: str
    message: str
    due_at: str
    status: ReminderStatus
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "message": self.message,
            "due_at": self.due_at,
            "status": str(self.status),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ReminderStore:
    def __init__(self, path: Path) -> None:
        self.path = path
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
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    message TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reminders_due
                    ON reminders(status, due_at);
                """
            )

    def create(self, name: str, message: str, due_at: str) -> Reminder:
        now = iso(utc_now())
        reminder = Reminder(
            id=uuid.uuid4().hex,
            name=name,
            message=message,
            due_at=due_at,
            status=ReminderStatus.SCHEDULED,
            created_at=now,
            updated_at=now,
        )
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO reminders(
                    id, name, message, due_at, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reminder.id,
                    reminder.name,
                    reminder.message,
                    reminder.due_at,
                    str(reminder.status),
                    reminder.created_at,
                    reminder.updated_at,
                ),
            )
        return reminder

    def get(self, reminder_id: str) -> Reminder | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
        return self._row_to_reminder(row) if row else None

    def list(self, limit: int = 100) -> list[Reminder]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM reminders ORDER BY due_at ASC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_reminder(row) for row in rows]

    def pending(self) -> list[Reminder]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT *
                FROM reminders
                WHERE status = ?
                ORDER BY due_at ASC
                """,
                (str(ReminderStatus.SCHEDULED),),
            ).fetchall()
        return [self._row_to_reminder(row) for row in rows]

    def update_status(self, reminder_id: str, status: ReminderStatus) -> Reminder | None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE reminders SET status = ?, updated_at = ? WHERE id = ?",
                (str(status), iso(utc_now()), reminder_id),
            )
            if cursor.rowcount == 0:
                return None
            row = self._connection.execute(
                "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
        return self._row_to_reminder(row)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _row_to_reminder(row: sqlite3.Row) -> Reminder:
        return Reminder(
            id=str(row["id"]),
            name=str(row["name"]),
            message=str(row["message"]),
            due_at=str(row["due_at"]),
            status=ReminderStatus(str(row["status"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


class ReminderService:
    def __init__(
        self,
        store: ReminderStore,
        scheduler: TaskScheduler,
        event_bus: EventBus,
    ) -> None:
        self.store = store
        self.scheduler = scheduler
        self.event_bus = event_bus
        self._jobs: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            for reminder in self.store.pending():
                self._schedule(reminder)

    def schedule(
        self,
        message: str,
        after_seconds: float,
        name: str | None = None,
    ) -> Reminder:
        message = message.strip()
        if not message:
            raise ValueError("Reminder message cannot be empty.")
        due_at = iso(utc_now() + timedelta(seconds=max(1.0, float(after_seconds))))
        reminder = self.store.create(
            name or f"reminder:{message[:40]}",
            message,
            due_at,
        )
        self._schedule(reminder)
        return reminder

    def cancel(self, reminder_id: str) -> bool:
        reminder = self.store.update_status(
            reminder_id,
            ReminderStatus.CANCELLED,
        )
        if reminder is None:
            return False
        job_id = self._jobs.pop(reminder_id, None)
        if job_id:
            self.scheduler.cancel(job_id)
        return True

    def pause(self, reminder_id: str) -> bool:
        reminder = self.store.update_status(reminder_id, ReminderStatus.PAUSED)
        if reminder is None:
            return False
        job_id = self._jobs.pop(reminder_id, None)
        if job_id:
            self.scheduler.cancel(job_id)
        return True

    def resume(self, reminder_id: str) -> bool:
        reminder = self.store.update_status(
            reminder_id,
            ReminderStatus.SCHEDULED,
        )
        if reminder is None:
            return False
        self._schedule(reminder)
        return True

    def list(self, limit: int = 100) -> list[Reminder]:
        return self.store.list(limit)

    def _schedule(self, reminder: Reminder) -> None:
        due_at = parse_iso(reminder.due_at)
        delay = max(0.0, (due_at - utc_now()).total_seconds())

        async def fire() -> str:
            current = self.store.get(reminder.id)
            if current is None or current.status != ReminderStatus.SCHEDULED:
                return ""
            self.store.update_status(reminder.id, ReminderStatus.COMPLETED)
            self._jobs.pop(reminder.id, None)
            await self.event_bus.emit_async(
                RuntimeEvent(
                    type=EventType.REMINDER_DUE,
                    data={
                        "id": reminder.id,
                        "name": reminder.name,
                        "message": reminder.message,
                    },
                )
            )
            return reminder.message

        job: ScheduledJob = self.scheduler.add_once(
            reminder.name,
            fire,
            run_after_seconds=delay,
            notify_on_complete=False,
        )
        self._jobs[reminder.id] = job.id
