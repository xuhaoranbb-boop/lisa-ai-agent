from pathlib import Path

from jarvis.memory import MemoryStore


def test_memory_round_trip_and_search(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "jarvis.db")
    try:
        stored = store.remember("The user prefers concise Chinese answers.", "preference")
        assert stored.id > 0
        assert [item.content for item in store.recall("Chinese answers")] == [
            stored.content
        ]
        assert store.forget(stored.id)
        assert store.recall("Chinese answers") == []
    finally:
        store.close()


def test_conversation_history_is_bounded_and_ordered(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "jarvis.db")
    try:
        store.add_message("session", "user", "one")
        store.add_message("session", "assistant", "two")
        store.add_message("session", "user", "three")

        history = store.history("session", limit=2)
        assert [message.content for message in history] == ["two", "three"]
        assert store.clear_history("session") == 3
    finally:
        store.close()


def test_context_recall_falls_back_to_recent_memory(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "jarvis.db")
    try:
        store.remember("I prefer concise Chinese answers.", "preference")
        results = store.recall_for_context("preferred answer style", limit=3)
        assert results
        assert results[0].content == "I prefer concise Chinese answers."
    finally:
        store.close()


def test_memory_importance_expiry_and_update(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "jarvis.db")
    try:
        low = store.remember("low priority", "general", importance=1)
        high = store.remember("high priority", "general", importance=5)
        expired = store.remember(
            "expired",
            "general",
            importance=5,
            expires_at="2000-01-01T00:00:00+00:00",
        )

        assert [item.id for item in store.list_memories()] == [high.id, low.id]
        assert store.expire_memories() == 1
        assert store.forget(expired.id) is False

        updated = store.update_memory(high.id, content="updated priority", importance=4)
        assert updated is not None
        assert updated.content == "updated priority"
        assert updated.importance == 4
    finally:
        store.close()


def test_task_memory_upserts_one_summary_per_task(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "jarvis.db")
    try:
        first = store.remember_task(
            "task-1",
            "Task completed: inspect project. Result: tests passed.",
        )
        second = store.remember_task(
            "task-1",
            "Task completed: inspect project. Result: build passed.",
        )

        assert second.id == first.id
        assert second.task_id == "task-1"
        assert second.category == "task"
        assert [item.id for item in store.list_memories()] == [first.id]
    finally:
        store.close()
