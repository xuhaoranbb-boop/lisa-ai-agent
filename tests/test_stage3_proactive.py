import pytest

from jarvis.runtime.events import EventBus, EventType
from jarvis.runtime.proactive import ProactiveEngine


@pytest.mark.asyncio
async def test_proactive_engine_triggers_and_respects_cooldown() -> None:
    bus = EventBus()
    engine = ProactiveEngine(bus, min_priority=3)
    try:
        first = await engine.emit_trigger(
            str(EventType.TASK_COMPLETE),
            {"task_id": "task-1", "goal": "检查项目", "message": "检查项目已完成。"},
        )
        second = await engine.emit_trigger(
            str(EventType.TASK_COMPLETE),
            {"task_id": "task-1", "goal": "检查项目", "message": "检查项目已完成。"},
        )

        assert len(first) == 1
        assert second == []
        notifications = [
            event
            for event in bus.history()
            if str(event.type) == str(EventType.PROACTIVE_NOTIFICATION)
        ]
        assert len(notifications) == 1
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_proactive_engine_quiet_hours_suppresses_noncritical_alerts() -> None:
    bus = EventBus()
    engine = ProactiveEngine(
        bus,
        min_priority=3,
        quiet_hours="00:00-23:59",
        timezone="UTC",
    )
    try:
        notifications = await engine.emit_trigger(
            str(EventType.TASK_COMPLETE),
            {"task_id": "task-1", "goal": "检查项目"},
        )
        assert notifications == []
    finally:
        await engine.stop()
