from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from jarvis.runtime.events import EventBus, EventType, RuntimeEvent
from jarvis.runtime.tasks import TaskEngine, TaskStatus

Condition = Callable[[dict[str, Any]], bool]
ProactiveAction = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class ProactiveRule:
    id: str
    event: str
    condition: Condition
    priority: int = 3
    cooldown_seconds: float = 300.0
    action: ProactiveAction | None = None
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProactiveNotification:
    rule_id: str
    event: str
    priority: int
    message: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "event": self.event,
            "priority": self.priority,
            "message": self.message,
            "data": self.data,
        }


class ProactiveEngine:
    """Event-driven proactive rules with cooldowns and task staleness checks."""

    def __init__(
        self,
        event_bus: EventBus,
        *,
        task_engine: TaskEngine | None = None,
        scan_interval_seconds: float = 60.0,
        stale_task_seconds: float = 1_800.0,
        min_priority: int = 3,
        quiet_hours: str = "",
        timezone: str = "UTC",
    ) -> None:
        self.event_bus = event_bus
        self.task_engine = task_engine
        self.scan_interval_seconds = max(5.0, scan_interval_seconds)
        self.stale_task_seconds = max(60.0, stale_task_seconds)
        self.min_priority = max(1, min(min_priority, 5))
        self.quiet_hours = _parse_quiet_hours(quiet_hours)
        self.timezone = timezone
        self._rules: dict[str, ProactiveRule] = {}
        self._last_fired: dict[str, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._scan_task: asyncio.Task[None] | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._register_defaults()

    def register(self, rule: ProactiveRule) -> None:
        self._rules[rule.id] = rule

    def unregister(self, rule_id: str) -> bool:
        return self._rules.pop(rule_id, None) is not None

    def rules(self) -> list[ProactiveRule]:
        return list(self._rules.values())

    async def start(self) -> None:
        if self._unsubscribe is None:
            self._unsubscribe = self.event_bus.subscribe(self._on_event)
        if self._scan_task is None and self.task_engine is not None:
            self._scan_task = asyncio.create_task(self._scan_loop())

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._scan_task is not None:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
            self._scan_task = None
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def emit_trigger(
        self,
        event: str,
        data: dict[str, Any],
    ) -> list[ProactiveNotification]:
        return await self._process(event, data)

    async def scan_tasks(self) -> list[ProactiveNotification]:
        if self.task_engine is None:
            return []
        now = datetime.now(UTC)
        notifications: list[ProactiveNotification] = []
        for task in self.task_engine.list_tasks(100):
            if task.status in {
                TaskStatus.COMPLETED,
                TaskStatus.CANCELLED,
                TaskStatus.FAILED,
            }:
                continue
            try:
                updated = datetime.fromisoformat(task.updated_at)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=UTC)
            except ValueError:
                continue
            age = (now - updated).total_seconds()
            if age < self.stale_task_seconds:
                continue
            notifications.extend(
                await self._process(
                    "TASK_STALE",
                    {
                        "task_id": task.id,
                        "goal": task.goal,
                        "status": str(task.status),
                        "age_seconds": age,
                        "message": f"任务“{task.goal}”已经较长时间没有进展。",
                    },
                )
            )
        return notifications

    def _on_event(self, event: RuntimeEvent) -> None:
        task = asyncio.create_task(self._process(str(event.type), event.to_dict()))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process(
        self,
        event: str,
        data: dict[str, Any],
    ) -> list[ProactiveNotification]:
        notifications: list[ProactiveNotification] = []
        now = time.monotonic()
        for rule in self._rules.values():
            if rule.event not in {event, "*"} or rule.priority < self.min_priority:
                continue
            if self._is_quiet_hours() and rule.priority < 5:
                continue
            try:
                if not rule.condition(data):
                    continue
            except Exception:
                continue
            cooldown_key = f"{rule.id}:{data.get('task_id') or data.get('job_id') or ''}"
            last_fired = self._last_fired.get(cooldown_key, 0.0)
            if now - last_fired < rule.cooldown_seconds:
                continue
            self._last_fired[cooldown_key] = now
            notification = ProactiveNotification(
                rule_id=rule.id,
                event=event,
                priority=rule.priority,
                message=str(data.get("message") or rule.description or event),
                data=data,
            )
            if rule.action is not None:
                try:
                    replacement = await rule.action(notification.to_dict())
                    if replacement:
                        notification = ProactiveNotification(
                            rule_id=rule.id,
                            event=event,
                            priority=rule.priority,
                            message=str(replacement.get("message") or notification.message),
                            data=replacement,
                        )
                except Exception as exc:
                    notification = ProactiveNotification(
                        rule_id=rule.id,
                        event=event,
                        priority=rule.priority,
                        message=f"主动提醒执行失败：{exc}",
                        data={**data, "error": str(exc)},
                    )
            await self.event_bus.emit_async(
                RuntimeEvent(
                    type=EventType.PROACTIVE_NOTIFICATION,
                    task_id=str(data.get("task_id") or "") or None,
                    data=notification.to_dict(),
                )
            )
            notifications.append(notification)
        return notifications

    async def _scan_loop(self) -> None:
        while True:
            await asyncio.sleep(self.scan_interval_seconds)
            try:
                await self.scan_tasks()
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    def _register_defaults(self) -> None:
        self.register(
            ProactiveRule(
                id="task_completed",
                event=str(EventType.TASK_COMPLETE),
                condition=lambda data: bool(data.get("goal")),
                priority=4,
                cooldown_seconds=30,
                description="任务已完成。",
            )
        )
        self.register(
            ProactiveRule(
                id="task_failed",
                event=str(EventType.TASK_FAILED),
                condition=lambda data: bool(data.get("error") or data.get("goal")),
                priority=5,
                cooldown_seconds=30,
                description="任务执行失败，需要检查。",
            )
        )
        self.register(
            ProactiveRule(
                id="reminder_due",
                event=str(EventType.REMINDER_DUE),
                condition=lambda data: bool(data.get("message")),
                priority=5,
                cooldown_seconds=1,
            )
        )
        self.register(
            ProactiveRule(
                id="task_stale",
                event="TASK_STALE",
                condition=lambda data: float(data.get("age_seconds") or 0)
                >= self.stale_task_seconds,
                priority=3,
                cooldown_seconds=self.stale_task_seconds,
            )
        )
        self.register(
            ProactiveRule(
                id="system_alert",
                event=str(EventType.SYSTEM_ALERT),
                condition=lambda data: bool(
                    isinstance(data.get("alert"), dict)
                    and data["alert"].get("message")
                ),
                priority=5,
                cooldown_seconds=900,
                action=self._system_alert_action,
            )
        )

    def _is_quiet_hours(self) -> bool:
        if self.quiet_hours is None:
            return False
        try:
            now = datetime.now(ZoneInfo(self.timezone))
        except Exception:
            now = datetime.now(UTC)
        current = now.hour * 60 + now.minute
        start, end = self.quiet_hours
        if start <= end:
            return start <= current < end
        return current >= start or current < end

    @staticmethod
    async def _system_alert_action(data: dict[str, Any]) -> dict[str, Any]:
        alert = data.get("alert") if isinstance(data.get("alert"), dict) else {}
        return {
            "message": str(alert.get("message") or "System warning."),
            "alert": alert,
        }


def _parse_quiet_hours(value: str) -> tuple[int, int] | None:
    value = value.strip()
    if not value or "-" not in value:
        return None
    start_text, end_text = value.split("-", 1)
    try:
        start_hour, start_minute = (int(part) for part in start_text.split(":", 1))
        end_hour, end_minute = (int(part) for part in end_text.split(":", 1))
    except (TypeError, ValueError):
        return None
    start = max(0, min(start_hour, 23)) * 60 + max(0, min(start_minute, 59))
    end = max(0, min(end_hour, 23)) * 60 + max(0, min(end_minute, 59))
    return start, end
