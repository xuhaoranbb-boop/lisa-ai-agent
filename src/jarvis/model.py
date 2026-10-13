from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from openai import AsyncOpenAI

from jarvis.config import Settings


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelReply:
    content: str
    tool_calls: list[ToolCall]
    raw_message: dict[str, Any]


@dataclass(frozen=True)
class StreamToolCallDelta:
    index: int
    id: str | None = None
    name: str | None = None
    arguments: str | None = None


@dataclass(frozen=True)
class ModelStreamChunk:
    content: str = ""
    tool_calls: tuple[StreamToolCallDelta, ...] = ()
    finish_reason: str | None = None


class ModelProvider(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply: ...


class OpenAICompatibleModel:
    name = "openai-compatible"

    def __init__(
        self,
        settings: Settings,
        *,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        self.extra_body = extra_body or {}
        self.client = AsyncOpenAI(
            api_key=settings.model_api_key.get_secret_value(),
            base_url=settings.model_base_url,
            timeout=settings.request_timeout,
            max_retries=2,
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        request = self._build_request(messages, tools)
        response = await self.client.chat.completions.create(**request)
        message = response.choices[0].message
        raw_message = message.model_dump(exclude_none=True)

        calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {"_raw": call.function.arguments}
            calls.append(
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=arguments,
                )
            )

        return ModelReply(
            content=message.content or "",
            tool_calls=calls,
            raw_message=raw_message,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        return await self.complete(messages, tools)

    async def function_call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        return await self.complete(messages, tools)

    async def vision(
        self,
        messages: list[dict[str, Any]],
        image: bytes,
    ) -> ModelReply:
        encoded = base64.b64encode(image).decode("ascii")
        vision_messages = [
            *messages,
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"},
                    }
                ],
            },
        ]
        return await self.complete(vision_messages, [])

    async def embedding(self, text: str) -> list[float]:
        if not self.settings.embedding_model.strip():
            raise NotImplementedError("No embedding model is configured.")
        response = await self.client.embeddings.create(
            model=self.settings.embedding_model,
            input=text,
        )
        return [float(value) for value in response.data[0].embedding]

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        async for chunk in self.stream_events(messages, tools or []):
            if chunk.content:
                yield chunk.content

    async def stream_events(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ModelStreamChunk]:
        request = self._build_request(messages, tools or [], stream=True)
        response = await self.client.chat.completions.create(**request)
        async for chunk in response:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            tool_calls = tuple(
                StreamToolCallDelta(
                    index=call.index,
                    id=call.id,
                    name=call.function.name if call.function else None,
                    arguments=call.function.arguments if call.function else None,
                )
                for call in (delta.tool_calls or [])
            )
            content = delta.content or ""
            if content or tool_calls or choice.finish_reason:
                yield ModelStreamChunk(
                    content=content,
                    tool_calls=tool_calls,
                    finish_reason=choice.finish_reason,
                )

    def _build_request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
        }
        if self.settings.reasoning_effort:
            request["reasoning_effort"] = self.settings.reasoning_effort
        extra_body: dict[str, Any] = dict(self.extra_body)
        if self.settings.keep_alive:
            extra_body["keep_alive"] = self.settings.keep_alive
        if extra_body:
            request["extra_body"] = extra_body
        if tools:
            request["tools"] = tools
        if stream:
            request["stream"] = True
        return request
