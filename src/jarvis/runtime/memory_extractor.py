from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from jarvis.memory import Memory, MemoryStore, is_sensitive_memory
from jarvis.runtime.semantic_memory import MemoryEmbeddingStore

_MEMORY_CUES = (
    "记住",
    "记得",
    "我的偏好",
    "我喜欢",
    "我不喜欢",
    "我希望",
    "以后",
    "默认",
    "我的项目",
    "我的习惯",
    "remember",
    "preference",
    "i like",
    "i prefer",
    "from now on",
)


@dataclass(frozen=True)
class MemoryCandidate:
    content: str
    category: str = "general"
    importance: int = 3
    confidence: float = 0.8
    summary: str = ""
    action: str = "add"
    memory_id: int | None = None
    metadata: dict[str, Any] | None = None


class MemoryExtractor:
    """Extract durable, non-sensitive facts after a completed turn."""

    def __init__(
        self,
        memory_store: MemoryStore,
        provider: Any | None = None,
        semantic_store: MemoryEmbeddingStore | None = None,
    ) -> None:
        self.memory_store = memory_store
        self.provider = provider
        self.semantic_store = semantic_store
        self.last_skip_reason = ""

    async def extract_and_store(
        self,
        user_text: str,
        assistant_text: str,
        *,
        session_id: str,
        task_id: str | None = None,
        owner_id: str = "local",
    ) -> list[Memory]:
        candidate_text = user_text.strip()
        if not candidate_text:
            return []
        if self._contains_sensitive(candidate_text):
            self.last_skip_reason = "sensitive-content"
            return []
        if not self._has_memory_signal(candidate_text):
            self.last_skip_reason = "not-durable"
            return []

        candidates = await self._extract_candidates(candidate_text, assistant_text)
        stored: list[Memory] = []
        for candidate in candidates:
            if self._contains_sensitive(candidate.content):
                continue
            memory = await self._store_candidate(
                candidate,
                session_id=session_id,
                task_id=task_id,
                owner_id=owner_id,
            )
            if memory is not None:
                stored.append(memory)
        return stored

    async def _extract_candidates(
        self,
        user_text: str,
        assistant_text: str,
    ) -> list[MemoryCandidate]:
        if self.provider is not None:
            try:
                reply = await self.provider.complete(
                    [
                        {
                            "role": "system",
                            "content": (
                                "Extract durable user memory from the latest user message. "
                                "Return only JSON. Never store passwords, API keys, tokens, "
                                "private credentials, transient requests, or casual chat. "
                                'Schema: {"memories":[{"content":"...","category":"preference|'
                                'project|person|commitment|general","importance":1-5,'
                                '"confidence":0-1,"summary":"..."}]}. '
                                "Return {\"memories\":[]} when nothing is worth remembering."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"User message: {user_text}\n"
                                f"Assistant reply: {assistant_text[:1_000]}"
                            ),
                        },
                    ],
                    [],
                )
                parsed = json.loads(reply.content)
                items = parsed.get("memories", []) if isinstance(parsed, dict) else []
                candidates = [
                    self._candidate_from_dict(item)
                    for item in items
                    if isinstance(item, dict)
                ]
                candidates = [item for item in candidates if item is not None]
                if candidates:
                    return candidates
            except Exception:
                pass
        return [self._heuristic_candidate(user_text)]

    async def _store_candidate(
        self,
        candidate: MemoryCandidate,
        *,
        session_id: str,
        task_id: str | None,
        owner_id: str,
    ) -> Memory | None:
        related = (
            await self.semantic_store.search(
                candidate.content,
                6,
                owner_id=owner_id,
                session_id=session_id,
            )
            if self.semantic_store is not None
            else self.memory_store.recall(
                candidate.content,
                6,
                owner_id=owner_id,
                session_id=session_id,
            )
        )
        duplicate = next(
            (
                memory
                for memory in related
                if _normalized(memory.content) == _normalized(candidate.content)
                or _token_overlap(memory.content, candidate.content) >= 0.9
            ),
            None,
        )
        if duplicate is not None:
            if candidate.action == "update" or len(candidate.content) > len(duplicate.content):
                memory = self.memory_store.update_memory(
                    duplicate.id,
                    content=candidate.content,
                    category=candidate.category,
                    importance=candidate.importance,
                    confidence=candidate.confidence,
                    summary=candidate.summary or candidate.content,
                    metadata=candidate.metadata or {},
                    session_id=session_id,
                    owner_id=owner_id,
                )
                if memory is not None and self.semantic_store is not None:
                    await self.semantic_store.index_memory(memory)
                return memory
            return duplicate

        memory = self.memory_store.remember(
            candidate.content,
            candidate.category,
            importance=candidate.importance,
            source="extractor",
            task_id=task_id,
            confidence=candidate.confidence,
            summary=candidate.summary or candidate.content,
            metadata=candidate.metadata or {},
            session_id=session_id,
            owner_id=owner_id,
        )
        if self.semantic_store is not None:
            await self.semantic_store.index_memory(memory)
        return memory

    @staticmethod
    def _candidate_from_dict(value: dict[str, Any]) -> MemoryCandidate | None:
        content = str(value.get("content") or "").strip()
        if not content:
            return None
        return MemoryCandidate(
            content=content,
            category=str(value.get("category") or "general").strip() or "general",
            importance=max(1, min(int(value.get("importance") or 3), 5)),
            confidence=max(0.0, min(float(value.get("confidence") or 0.8), 1.0)),
            summary=str(value.get("summary") or content).strip(),
            action=str(value.get("action") or "add"),
            memory_id=(
                int(value["memory_id"])
                if value.get("memory_id") is not None
                else None
            ),
            metadata=dict(value.get("metadata") or {}),
        )

    @staticmethod
    def _heuristic_candidate(text: str) -> MemoryCandidate:
        lowered = text.lower()
        if any(term in lowered for term in ("我喜欢", "我不喜欢", "偏好", "prefer", "i like")):
            category = "preference"
            importance = 4
        elif any(term in lowered for term in ("我的项目", "项目", "project")):
            category = "project"
            importance = 4
        elif any(term in lowered for term in ("承诺", "约定", "提醒我", "commitment")):
            category = "commitment"
            importance = 4
        elif any(term in lowered for term in ("我是", "我的名字", "my name")):
            category = "person"
            importance = 5
        else:
            category = "general"
            importance = 3
        return MemoryCandidate(
            content=text.strip(),
            category=category,
            importance=importance,
            confidence=0.72,
            summary=text.strip()[:240],
            metadata={"extractor": "heuristic"},
        )

    @staticmethod
    def _has_memory_signal(text: str) -> bool:
        lowered = text.lower()
        return any(cue in lowered for cue in _MEMORY_CUES)

    @staticmethod
    def _contains_sensitive(text: str) -> bool:
        return is_sensitive_memory(text)


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _token_overlap(left: str, right: str) -> float:
    left_tokens = set(re.findall(r"[\w\u4e00-\u9fff]+", left.lower()))
    right_tokens = set(re.findall(r"[\w\u4e00-\u9fff]+", right.lower()))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
