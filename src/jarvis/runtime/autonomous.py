from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from jarvis.runtime.budget import TokenBudgetManager
from jarvis.runtime.cancellation import CancellationToken
from jarvis.runtime.context import ContextEngine
from jarvis.runtime.execution import AgentExecutionEngine, ExecutionRecord
from jarvis.runtime.multimodal import AmbientContext
from jarvis.runtime.tasks import TaskStep


@dataclass(frozen=True)
class AutonomyBoundary:
    max_steps: int = 20
    timeout_seconds: float = 300.0
    token_budget: int = 120_000
    permission_boundary: str = "PermissionManager"
    stop_condition: str = "task_complete_or_blocked"


class AutonomousAgentLoop:
    """Bounded wrapper around AgentExecutionEngine with time and token limits."""

    def __init__(
        self,
        *,
        execution_engine: AgentExecutionEngine,
        context_engine: ContextEngine | None = None,
        ambient_context: AmbientContext | None = None,
        budget_manager: TokenBudgetManager | None = None,
    ) -> None:
        self.execution_engine = execution_engine
        self.context_engine = context_engine
        self.ambient_context = ambient_context
        self.budget_manager = budget_manager or execution_engine.budget_manager

    async def run(
        self,
        goal: str,
        *,
        session_id: str,
        plan: list[TaskStep] | None = None,
        boundary: AutonomyBoundary | None = None,
        cancellation: CancellationToken | None = None,
        execution_id: str | None = None,
    ) -> ExecutionRecord:
        boundary = boundary or AutonomyBoundary()
        cancellation = cancellation or CancellationToken()
        self.execution_engine.max_steps = min(
            self.execution_engine.max_steps,
            max(1, boundary.max_steps),
        )
        if not self.budget_manager.can_spend(
            min(boundary.token_budget, self.budget_manager.request_token_budget),
            task_id="",
            request_id="",
        ):
            raise RuntimeError("Autonomous loop token budget is unavailable.")
        if self.ambient_context is not None:
            await self.ambient_context.snapshot()
        started = time.perf_counter()
        try:
            return await asyncio.wait_for(
                self.execution_engine.run(
                    goal,
                    session_id=session_id,
                    plan=plan,
                    cancellation=cancellation,
                    execution_id=execution_id,
                ),
                timeout=max(1.0, boundary.timeout_seconds),
            )
        except TimeoutError:
            cancellation.cancel("Autonomous loop timed out.")
            raise TimeoutError(
                f"Autonomous loop exceeded {boundary.timeout_seconds:.0f}s."
            ) from None
        finally:
            if self.ambient_context is not None:
                self.ambient_context.record_action(
                    "autonomous_loop",
                    {
                        "goal": goal,
                        "elapsed_seconds": time.perf_counter() - started,
                    },
                )
