from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from jarvis.memory import Memory, MemoryStore
from jarvis.runtime.cache import TTLCache


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _recency_score(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
    except ValueError:
        return 0.0
    age_days = max(0.0, (datetime.now(UTC) - timestamp).total_seconds() / 86_400)
    return 1.0 / (1.0 + age_days / 30.0)


class MemoryEmbeddingStore:
    """Semantic memory search with deterministic FTS fallback."""

    def __init__(
        self,
        memory_store: MemoryStore,
        provider: Any | None = None,
        *,
        model_name: str = "",
        cache_ttl_seconds: float = 30.0,
    ) -> None:
        self.memory_store = memory_store
        self.provider = provider
        self.model_name = model_name or getattr(provider, "name", "embedding")
        self.last_error = ""
        self.cache: TTLCache[tuple[str, str | None, str | None], list[Memory]] = TTLCache(
            ttl_seconds=cache_ttl_seconds,
            max_entries=500,
        )

    async def index_memory(self, memory: Memory) -> bool:
        if self.provider is None:
            return False
        try:
            embedding = await self.provider.embedding(memory.summary or memory.content)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        if not embedding:
            return False
        self.memory_store.set_embedding(memory.id, self.model_name, embedding)
        self.cache.clear()
        return True

    async def reindex(
        self,
        *,
        owner_id: str | None = None,
        session_id: str | None = None,
    ) -> int:
        indexed = 0
        for memory in self.memory_store.list_memories(
            500,
            owner_id=owner_id,
            session_id=session_id,
        ):
            if await self.index_memory(memory):
                indexed += 1
        return indexed

    async def search(
        self,
        query: str,
        limit: int = 6,
        *,
        owner_id: str | None = None,
        session_id: str | None = None,
    ) -> list[Memory]:
        query = query.strip()
        if not query:
            return []
        cache_key = (query, owner_id, session_id)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        if self.provider is not None:
            try:
                query_embedding = await self.provider.embedding(query)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                query_embedding = []
            if query_embedding:
                candidates = self.memory_store.list_embeddings(
                    owner_id=owner_id,
                    session_id=session_id,
                )
                ranked: list[tuple[float, Memory]] = []
                for memory, embedding in candidates:
                    semantic = max(0.0, _cosine_similarity(query_embedding, embedding))
                    score = (
                        semantic * 0.65
                        + (memory.importance / 5.0) * 0.20
                        + _recency_score(memory.last_used_at or memory.updated_at) * 0.10
                        + memory.confidence * 0.05
                    )
                    if semantic > 0 or score > 0.25:
                        ranked.append((score, memory))
                if ranked:
                    ranked.sort(key=lambda item: item[0], reverse=True)
                    results = [memory for _score, memory in ranked[:limit]]
                    self.memory_store.mark_used([memory.id for memory in results])
                    self.cache.set(cache_key, results)
                    return results

        results = self.memory_store.recall(
            query,
            limit,
            owner_id=owner_id,
            session_id=session_id,
        )
        self.cache.set(cache_key, results)
        return results
