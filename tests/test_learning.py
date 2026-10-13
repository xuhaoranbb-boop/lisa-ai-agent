from pathlib import Path

from jarvis.learning import chunk_text, learn_paths, normalize_extensions
from jarvis.memory import MemoryStore


def test_chunk_text_respects_paragraphs_and_max_size() -> None:
    text = "short\n\n" + ("x" * 3000)
    chunks = chunk_text(text, max_chars=1200)
    assert len(chunks) >= 3
    assert chunks[0] == "short"
    assert all(len(chunk) <= 1200 for chunk in chunks)


def test_learn_paths_imports_text_and_replaces_old_knowledge(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "jarvis.db")
    source = tmp_path / "guide.md"
    source.write_text(
        "Jarvis should answer in concise Chinese.\n\nSecond paragraph.",
        encoding="utf-8",
    )

    first = learn_paths(
        store,
        [source],
        normalize_extensions(".md"),
        chunk_size=1200,
        max_bytes=256_000,
    )
    assert first.files_learned == 1
    assert first.chunks_added == 1

    second = learn_paths(
        store,
        [source],
        normalize_extensions(".md"),
        chunk_size=1200,
        max_bytes=256_000,
        replace=True,
    )
    assert second.chunks_added == 1
    matches = store.recall("concise Chinese", limit=10)
    assert len(matches) == 1
    store.close()


def test_remember_dedupes_exact_content(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "jarvis.db")
    first = store.remember("The user prefers concise answers.", "preference")
    second = store.remember("The user prefers concise answers.", "preference")
    assert second.id == first.id
    assert len(store.list_memories()) == 1
    store.close()
