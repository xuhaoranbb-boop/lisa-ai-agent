from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for character in text if "\u4e00" <= character <= "\u9fff")
    non_cjk = max(0, len(text) - cjk)
    return max(1, cjk + math.ceil(non_cjk / 4))


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    embedding_tokens: int = 0
    estimated_cost: float = 0.0
    requests: int = 0

    def add(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        embedding_tokens: int = 0,
        cost: float = 0.0,
    ) -> None:
        self.input_tokens += max(0, input_tokens)
        self.output_tokens += max(0, output_tokens)
        self.embedding_tokens += max(0, embedding_tokens)
        self.estimated_cost += max(0.0, cost)
        self.requests += 1

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.embedding_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "embedding_tokens": self.embedding_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost": round(self.estimated_cost, 6),
            "requests": self.requests,
        }


class TokenBudgetManager:
    """Track estimated model usage with request, task, and daily budgets."""

    def __init__(
        self,
        *,
        request_token_budget: int = 12_000,
        task_token_budget: int = 120_000,
        daily_token_budget: int = 500_000,
        input_cost_per_million: float = 0.0,
        output_cost_per_million: float = 0.0,
        downshift_ratio: float = 0.8,
    ) -> None:
        self.request_token_budget = max(0, request_token_budget)
        self.task_token_budget = max(0, task_token_budget)
        self.daily_token_budget = max(0, daily_token_budget)
        self.input_cost_per_million = max(0.0, input_cost_per_million)
        self.output_cost_per_million = max(0.0, output_cost_per_million)
        self.downshift_ratio = min(1.0, max(0.1, downshift_ratio))
        self._tasks: dict[str, TokenUsage] = {}
        self._requests: dict[str, TokenUsage] = {}
        self._daily: dict[str, TokenUsage] = {}
        self._sessions: dict[str, TokenUsage] = {}
        self._models: dict[str, TokenUsage] = {}

    def can_spend(
        self,
        estimated_tokens: int,
        *,
        task_id: str = "",
        request_id: str = "",
        session_id: str = "",
        model: str = "",
    ) -> bool:
        return not self._exceeded(
            estimated_tokens,
            task_id=task_id,
            request_id=request_id,
        )

    def should_downshift(self, *, threshold: float | None = None) -> bool:
        ratio = self.downshift_ratio if threshold is None else threshold
        if not self.daily_token_budget:
            return False
        used = self.daily_usage().total_tokens
        return used >= self.daily_token_budget * min(1.0, max(0.1, ratio))

    def record(
        self,
        *,
        input_text: str = "",
        output_text: str = "",
        embedding_text: str = "",
        task_id: str = "",
        request_id: str = "",
        session_id: str = "",
        model: str = "",
    ) -> TokenUsage:
        input_tokens = estimate_tokens(input_text)
        output_tokens = estimate_tokens(output_text)
        embedding_tokens = estimate_tokens(embedding_text)
        cost = (
            input_tokens / 1_000_000 * self.input_cost_per_million
            + output_tokens / 1_000_000 * self.output_cost_per_million
        )
        usage = TokenUsage()
        usage.add(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            embedding_tokens=embedding_tokens,
            cost=cost,
        )
        if task_id:
            self._merge(self._tasks, task_id, usage)
        if request_id:
            self._requests[request_id] = usage
        if session_id:
            self._merge(self._sessions, session_id, usage)
        if model:
            self._merge(self._models, model, usage)
        self._merge(self._daily, _day_key(), usage)
        self._trim(self._requests, 2_000)
        self._trim(self._sessions, 500)
        self._trim(self._models, 100)
        return usage

    def task_usage(self, task_id: str) -> TokenUsage:
        return self._tasks.get(task_id, TokenUsage())

    def request_usage(self, request_id: str) -> TokenUsage:
        return self._requests.get(request_id, TokenUsage())

    def daily_usage(self) -> TokenUsage:
        return self._daily.get(_day_key(), TokenUsage())

    def context_budget(self, task_id: str = "") -> int:
        if not self.task_token_budget:
            return self.request_token_budget
        remaining = self.task_token_budget - self.task_usage(task_id).total_tokens
        return max(0, min(self.request_token_budget, remaining))

    def snapshot(self, task_id: str = "") -> dict[str, Any]:
        return {
            "request_budget": self.request_token_budget,
            "task_budget": self.task_token_budget,
            "daily_budget": self.daily_token_budget,
            "task": self.task_usage(task_id).to_dict(),
            "daily": self.daily_usage().to_dict(),
            "sessions": {
                session_id: usage.to_dict()
                for session_id, usage in self._sessions.items()
            },
            "models": {
                model: usage.to_dict()
                for model, usage in self._models.items()
            },
        }

    def _exceeded(
        self,
        estimated_tokens: int,
        *,
        task_id: str,
        request_id: str,
    ) -> bool:
        if self.request_token_budget and estimated_tokens > self.request_token_budget:
            return True
        if (
            task_id
            and self.task_token_budget
            and self.task_usage(task_id).total_tokens + estimated_tokens
            > self.task_token_budget
        ):
            return True
        return bool(
            self.daily_token_budget
            and self.daily_usage().total_tokens + estimated_tokens
            > self.daily_token_budget
        )

    @staticmethod
    def _merge(
        target: dict[str, TokenUsage],
        key: str,
        usage: TokenUsage,
    ) -> None:
        current = target.setdefault(key, TokenUsage())
        current.add(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            embedding_tokens=usage.embedding_tokens,
            cost=usage.estimated_cost,
        )

    @staticmethod
    def _trim(values: dict[str, TokenUsage], limit: int) -> None:
        if len(values) <= limit:
            return
        for key in list(values)[: len(values) - limit]:
            values.pop(key, None)


def _day_key() -> str:
    return datetime.now(UTC).date().isoformat()


class TokenTracker(TokenBudgetManager):
    """Backward-compatible phase 6 name for the token usage manager."""
