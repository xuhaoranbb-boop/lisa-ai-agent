from pathlib import Path

import pytest

from jarvis.config import Settings
from jarvis.memory import MemoryStore
from jarvis.model import ModelReply
from jarvis.runtime.memory_extractor import MemoryExtractor
from jarvis.runtime.semantic_memory import MemoryEmbeddingStore


class ExtractorProvider:
    async def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        return ModelReply(
            content=(
                '{"memories":[{"content":"用户喜欢简洁回答",'
                '"category":"preference","importance":4,"confidence":0.91,'
                '"summary":"简洁偏好"}]}'
            ),
            tool_calls=[],
            raw_message={"role": "assistant", "content": "memory"},
        )

    async def embedding(self, text: str) -> list[float]:
        return [1.0, 0.0]


@pytest.mark.asyncio
async def test_memory_extractor_stores_confident_durable_memory(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path)
    store = MemoryStore(settings.database_path)
    semantic = MemoryEmbeddingStore(store, ExtractorProvider(), model_name="fake")
    extractor = MemoryExtractor(store, ExtractorProvider(), semantic)
    try:
        memories = await extractor.extract_and_store(
            "记住，我以后喜欢简洁回答。",
            "好的。",
            session_id="session-1",
        )
        assert len(memories) == 1
        assert memories[0].confidence == pytest.approx(0.91)
        assert memories[0].category == "preference"
        assert memories[0].summary == "简洁偏好"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_memory_extractor_rejects_secrets_and_repeated_memory(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path)
    store = MemoryStore(settings.database_path)
    extractor = MemoryExtractor(store, ExtractorProvider())
    try:
        assert await extractor.extract_and_store(
            "记住我的 API key 是 sk-1234567890abcdefghijklmn",
            "不会保存。",
            session_id="session-1",
        ) == []
        first = await extractor.extract_and_store(
            "记住我喜欢简洁回答。",
            "好的。",
            session_id="session-1",
        )
        second = await extractor.extract_and_store(
            "记住我喜欢简洁回答。",
            "好的。",
            session_id="session-1",
        )
        assert len(first) == 1
        assert len(second) == 1
        assert second[0].id == first[0].id
        assert len(store.list_memories(owner_id="local", session_id="session-1")) == 1
    finally:
        store.close()
