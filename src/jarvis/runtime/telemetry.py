from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from jarvis.runtime.events import EventBus, EventType, RuntimeEvent


@dataclass(frozen=True)
class TimingSample:
    name: str
    elapsed_ms: float
    started_at: float
    data: dict[str, Any] = field(default_factory=dict)


class TimingRecorder:
    """Record named stages and publish them as runtime events."""

    def __init__(self, event_bus: EventBus | None = None) -> None:
        self.event_bus = event_bus
        self.samples: list[TimingSample] = []

    def start(self) -> float:
        return time.perf_counter()

    def mark(
        self,
        name: str,
        started_at: float,
        *,
        task_id: str | None = None,
        request_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> TimingSample:
        sample = TimingSample(
            name=name,
            elapsed_ms=round((time.perf_counter() - started_at) * 1_000, 3),
            started_at=started_at,
            data=data or {},
        )
        self.samples.append(sample)
        if self.event_bus is not None:
            event_data = {"name": sample.name, "elapsed_ms": sample.elapsed_ms, **sample.data}
            self.event_bus.emit(
                RuntimeEvent(
                    type=EventType(name) if name in EventType._value2member_map_ else name,
                    data=event_data,
                    task_id=task_id,
                    request_id=request_id,
                )
            )
        return sample
