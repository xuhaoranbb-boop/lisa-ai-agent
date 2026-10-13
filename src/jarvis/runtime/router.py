from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from pydantic import SecretStr

from jarvis.config import Settings
from jarvis.model import ModelReply, ModelStreamChunk, OpenAICompatibleModel
from jarvis.runtime.budget import TokenBudgetManager


class ModelRole(StrEnum):
    CHAT = "chat"
    VOICE = "voice"
    REASONING = "reasoning"
    CODE = "code"
    VISION = "vision"
    COMPUTER = "computer"
    EMBEDDING = "embedding"


@dataclass(frozen=True)
class ModelRoute:
    role: ModelRole
    model: str
    base_url: str
    api_key: str
    local: bool
    temperature: float
    max_tokens: int
    reasoning_effort: str | None
    keep_alive: str
    extra_body: dict[str, Any] | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.base_url, self.model, self.api_key)


class ModelRouter:
    """Resolve a model role to an isolated OpenAI-compatible provider."""

    def __init__(
        self,
        settings: Settings,
        budget_manager: TokenBudgetManager | None = None,
    ) -> None:
        self.settings = settings
        self.budget_manager = budget_manager
        self._providers: dict[tuple[str, str, str], OpenAICompatibleModel] = {}

    def route(self, role: ModelRole | str = ModelRole.CHAT) -> ModelRoute:
        resolved_role = ModelRole(role)
        settings = self.settings
        model = settings.model
        base_url = settings.model_base_url
        api_key = settings.model_api_key.get_secret_value()
        temperature = settings.temperature
        max_tokens = settings.max_tokens

        if resolved_role == ModelRole.VOICE:
            model = settings.voice_model
            max_tokens = settings.voice_max_tokens
        elif resolved_role == ModelRole.REASONING:
            model = settings.reasoning_model or settings.model
        elif resolved_role == ModelRole.CODE:
            model = settings.code_model or settings.model
        elif resolved_role == ModelRole.VISION:
            model = settings.vision_model or settings.model
            base_url = settings.vision_base_url or settings.model_base_url
            api_key = (
                settings.vision_api_key.get_secret_value()
                or settings.model_api_key.get_secret_value()
            )
        elif resolved_role == ModelRole.EMBEDDING:
            model = settings.embedding_model or settings.model
            max_tokens = 1
        elif resolved_role == ModelRole.COMPUTER:
            model = settings.computer_model or settings.model

        return ModelRoute(
            role=resolved_role,
            model=model,
            base_url=base_url,
            api_key=api_key,
            local=self._is_local(base_url),
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=settings.reasoning_effort,
            keep_alive=settings.keep_alive,
            extra_body=None,
        )

    def provider_for(self, role: ModelRole | str = ModelRole.CHAT) -> OpenAICompatibleModel:
        routes = self.routes_for(role)
        providers = [self._provider_for_route(route) for route in routes]
        primary = (
            providers[0]
            if len(providers) == 1
            else FailoverModelProvider(providers)
        )
        budget_model = self.settings.model_budget_fallback.strip()
        if (
            self.budget_manager is not None
            and budget_model
            and budget_model != routes[0].model
        ):
            cheap_route = ModelRoute(
                **{
                    **routes[0].__dict__,
                    "model": budget_model,
                }
            )
            cheap = self._provider_for_route(cheap_route)
            return BudgetAwareModelProvider(  # type: ignore[return-value]
                primary,
                cheap,
                self.budget_manager,
                threshold=self.settings.budget_downshift_ratio,
            )
        return primary  # type: ignore[return-value]

    def routes_for(self, role: ModelRole | str = ModelRole.CHAT) -> list[ModelRoute]:
        primary = self.route(role)
        resolved_role = ModelRole(role)
        fallback_roles = {
            item.strip()
            for item in self.settings.model_fallback_roles.split(",")
            if item.strip()
        }
        fallback_names = [
            item.strip()
            for item in self.settings.model_fallbacks.split(",")
            if item.strip()
        ]
        if fallback_roles and resolved_role.value not in fallback_roles:
            fallback_names = []
        fallback_base_url = self.settings.model_fallback_base_url or primary.base_url
        fallback_api_key = (
            self.settings.model_fallback_api_key.get_secret_value()
            or primary.api_key
        )
        fallback_extra_body = _parse_extra_body(self.settings.model_fallback_extra_body)
        return [
            primary,
            *[
                replace(
                    primary,
                    model=model,
                    base_url=fallback_base_url,
                    api_key=fallback_api_key,
                    local=self._is_local(fallback_base_url),
                    extra_body=fallback_extra_body,
                )
                for model in fallback_names
                if model != primary.model
            ],
        ]

    def _provider_for_route(self, route: ModelRoute) -> OpenAICompatibleModel:
        provider = self._providers.get(route.key)
        if provider is not None:
            return provider

        route_settings = self.settings.model_copy(
            update={
                "model": route.model,
                "model_base_url": route.base_url,
                "model_api_key": SecretStr(route.api_key),
                "temperature": route.temperature,
                "max_tokens": route.max_tokens,
                "reasoning_effort": route.reasoning_effort,
                "keep_alive": route.keep_alive,
            }
        )
        provider = OpenAICompatibleModel(route_settings, extra_body=route.extra_body)
        self._providers[route.key] = provider
        return provider

    @staticmethod
    def _is_local(base_url: str) -> bool:
        lowered = base_url.lower()
        return "127.0.0.1" in lowered or "localhost" in lowered or "0.0.0.0" in lowered


def _parse_extra_body(raw: str) -> dict[str, Any] | None:
    """Parse a JSON object string for per-provider extra request body fields."""
    value = (raw or "").strip()
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class FailoverModelProvider:
    name = "failover"

    def __init__(self, providers: list[OpenAICompatibleModel]) -> None:
        self.providers = providers
        self.last_error = ""

    async def complete(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelReply:
        last_error: Exception | None = None
        for provider in self.providers:
            try:
                return await provider.complete(messages, tools)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                last_error = exc
        raise RuntimeError(f"All model providers failed: {last_error}") from last_error

    async def stream(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[str]:
        async for chunk in self.stream_events(messages, tools or []):
            if chunk.content:
                yield chunk.content

    async def stream_events(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[ModelStreamChunk]:
        last_error: Exception | None = None
        for provider in self.providers:
            yielded = False
            try:
                async for chunk in provider.stream_events(messages, tools or []):
                    yielded = True
                    yield chunk
                return
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                last_error = exc
                if yielded:
                    raise
        raise RuntimeError(f"All model providers failed: {last_error}") from last_error

    async def vision(
        self,
        messages: list[dict[str, object]],
        image: bytes,
    ) -> ModelReply:
        last_error: Exception | None = None
        for provider in self.providers:
            try:
                return await provider.vision(messages, image)
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"All vision providers failed: {last_error}") from last_error

    async def embedding(self, text: str) -> list[float]:
        last_error: Exception | None = None
        for provider in self.providers:
            try:
                return await provider.embedding(text)
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"All embedding providers failed: {last_error}") from last_error


class BudgetAwareModelProvider:
    """Choose a lower-cost model once the daily budget crosses the threshold."""

    name = "budget-aware"

    def __init__(
        self,
        primary: OpenAICompatibleModel,
        low_cost: OpenAICompatibleModel,
        budget_manager: TokenBudgetManager,
        *,
        threshold: float = 0.8,
    ) -> None:
        self.primary = primary
        self.low_cost = low_cost
        self.budget_manager = budget_manager
        self.threshold = threshold

    def _selected(self) -> OpenAICompatibleModel:
        if self.budget_manager.should_downshift(threshold=self.threshold):
            return self.low_cost
        return self.primary

    async def complete(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelReply:
        selected = self._selected()
        try:
            return await selected.complete(messages, tools)
        except Exception:
            if selected is self.low_cost:
                return await self.primary.complete(messages, tools)
            raise

    async def stream(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[str]:
        async for chunk in self.stream_events(messages, tools or []):
            if chunk.content:
                yield chunk.content

    async def stream_events(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[ModelStreamChunk]:
        selected = self._selected()
        try:
            async for chunk in selected.stream_events(messages, tools or []):
                yield chunk
        except Exception:
            if selected is not self.low_cost:
                raise
            async for chunk in self.primary.stream_events(messages, tools or []):
                yield chunk

    async def vision(
        self,
        messages: list[dict[str, object]],
        image: bytes,
    ) -> ModelReply:
        selected = self._selected()
        try:
            return await selected.vision(messages, image)
        except Exception:
            if selected is self.low_cost:
                return await self.primary.vision(messages, image)
            raise

    async def embedding(self, text: str) -> list[float]:
        selected = self._selected()
        try:
            return await selected.embedding(text)
        except Exception:
            if selected is self.low_cost:
                return await self.primary.embedding(text)
            raise
