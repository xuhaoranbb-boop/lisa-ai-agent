from pathlib import Path

import pytest

from jarvis.config import Settings
from jarvis.memory import MemoryStore, Message
from jarvis.model import ModelReply
from jarvis.persona import PersonaManager
from jarvis.runtime.context import ContextEngine, ReferenceResolver
from jarvis.runtime.semantic_memory import MemoryEmbeddingStore
from jarvis.runtime.tasks import TaskEngine, TaskStep, TaskStore


class FakeProvider:
    async def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        return ModelReply(
            content="摘要：保留当前目标、偏好、决定和未完成任务。",
            tool_calls=[],
            raw_message={"role": "assistant", "content": "summary"},
        )

    async def embedding(self, text: str) -> list[float]:
        return [1.0, 0.0] if "茶" in text else [0.0, 1.0]


def test_reference_resolver_handles_ordinal_and_continue() -> None:
    store = TaskStore(Path(":memory:"))
    try:
        import asyncio

        engine = TaskEngine(store)
        task = asyncio.run(
            engine.create_task("检查项目", session_id="session-1")
        )
        task = asyncio.run(
            engine.set_plan(
                task.id,
                [
                    TaskStep(id="one", title="打开项目"),
                    TaskStep(id="two", title="运行测试"),
                ],
            )
        )
        resolver = ReferenceResolver()

        second = resolver.resolve_reference(
            "执行第二个",
            history=[],
            task=task,
        )
        assert second is not None
        assert second.target_id == "two"
        assert second.value == "运行测试"

        continued = resolver.resolve_reference(
            "继续刚才的任务",
            history=[],
            task=task,
        )
        assert continued is not None
        assert continued.target_type == "task"

        latest = resolver.resolve_reference(
            "刚才那个再处理一下",
            history=[
                Message(
                    id=10,
                    session_id="session-1",
                    role="assistant",
                    content="刚才生成了测试方案。",
                    created_at="2026-09-12T00:00:00+00:00",
                )
            ],
            task=task,
        )
        assert latest is not None
        assert latest.target_type == "assistant_message"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_context_engine_compresses_history_and_injects_persona(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace=tmp_path,
        context_max_chars=2_000,
    )
    memory = MemoryStore(settings.database_path)
    task_store = TaskStore(settings.database_path)
    task_engine = TaskEngine(task_store)
    persona = PersonaManager(settings)
    try:
        task = await task_engine.create_task("检查项目", session_id="session-1")
        for index in range(5):
            memory.add_message("session-1", "user", f"消息{index}:" + "长" * 1_200)
            memory.add_message("session-1", "assistant", f"回复{index}:" + "答" * 1_200)
        context = ContextEngine(
            memory,
            task_engine,
            persona,
            provider=FakeProvider(),
            max_context_chars=2_000,
        )

        snapshot = await context.build_context(
            "刚才那个继续一下",
            session_id="session-1",
            task_id=task.id,
        )

        assert "摘要：" in snapshot.summary
        assert snapshot.covered_message_id > 0
        assert "Persona profile:" in snapshot.persona_prompt
        assert snapshot.task is not None
        assert snapshot.goal == "检查项目"
    finally:
        memory.close()
        task_store.close()


@pytest.mark.asyncio
async def test_semantic_memory_and_fts_fallback(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path)
    store = MemoryStore(settings.database_path)
    try:
        tea = store.remember(
            "用户喜欢喝茶",
            "preference",
            importance=5,
            confidence=0.9,
            owner_id="user-1",
            session_id="session-1",
        )
        coffee = store.remember(
            "用户可能喜欢咖啡",
            "preference",
            importance=2,
            confidence=0.4,
            owner_id="user-1",
            session_id="session-1",
        )
        provider = FakeProvider()
        semantic = MemoryEmbeddingStore(store, provider, model_name="fake")
        await semantic.index_memory(tea)
        await semantic.index_memory(coffee)

        results = await semantic.search(
            "茶",
            owner_id="user-1",
            session_id="session-1",
        )
        assert results[0].id == tea.id

        fallback = MemoryEmbeddingStore(store, None)
        fallback_results = await fallback.search(
            "咖啡",
            owner_id="user-1",
            session_id="session-1",
        )
        assert coffee.id in {item.id for item in fallback_results}
    finally:
        store.close()


def test_memory_scope_is_isolated_by_owner_and_session(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path)
    store = MemoryStore(settings.database_path)
    try:
        store.remember("session one", owner_id="user-1", session_id="one")
        store.remember("session two", owner_id="user-1", session_id="two")
        store.remember("global user one", owner_id="user-1", session_id=None)
        store.remember("other user", owner_id="user-2", session_id="one")

        results = store.list_memories(owner_id="user-1", session_id="one")
        contents = {item.content for item in results}
        assert "session one" in contents
        assert "global user one" in contents
        assert "session two" not in contents
        assert "other user" not in contents
    finally:
        store.close()
