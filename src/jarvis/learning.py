from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from jarvis.memory import MemoryStore

DEFAULT_TEXT_EXTENSIONS = {
    ".cfg",
    ".conf",
    ".ini",
    ".json",
    ".log",
    ".md",
    ".markdown",
    ".py",
    ".rst",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

SKIP_DIRECTORY_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    "artifacts",
    "node_modules",
}


@dataclass(frozen=True)
class TrainingReport:
    files_learned: int
    chunks_added: int
    files_skipped: int


def normalize_extensions(value: str) -> set[str]:
    extensions = set()
    for item in value.replace(";", ",").split(","):
        item = item.strip().lower()
        if not item:
            continue
        if not item.startswith("."):
            item = f".{item}"
        extensions.add(item)
    return extensions or DEFAULT_TEXT_EXTENSIONS


def iter_text_files(
    paths: Iterable[Path],
    extensions: set[str],
) -> Iterable[Path]:
    seen: set[Path] = set()
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        if path in seen:
            continue
        seen.add(path)

        if path.is_file():
            if path.suffix.lower() in extensions:
                yield path
            continue

        if not path.is_dir():
            continue

        for child in sorted(path.rglob("*")):
            if any(part in SKIP_DIRECTORY_NAMES for part in child.parts):
                continue
            if child.is_file() and child.suffix.lower() in extensions:
                yield child


def chunk_text(text: str, max_chars: int = 1200) -> list[str]:
    """Split text into context-sized chunks while preserving blank-line boundaries."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    paragraphs = [part.strip() for part in normalized.split("\n\n") if part.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0

    def flush() -> None:
        nonlocal current, current_size
        if current:
            chunks.append("\n\n".join(current))
        current = []
        current_size = 0

    for paragraph in paragraphs:
        for piece in _split_long_paragraph(paragraph, max_chars):
            added_size = len(piece) + (2 if current else 0)
            if current and current_size + added_size > max_chars:
                flush()
            current.append(piece)
            current_size += len(piece) + (2 if len(current) > 1 else 0)

    flush()
    return chunks


def _split_long_paragraph(paragraph: str, max_chars: int) -> list[str]:
    if len(paragraph) <= max_chars:
        return [paragraph]

    lines = paragraph.splitlines()
    pieces: list[str] = []
    current: list[str] = []
    current_size = 0

    for line in lines:
        if current and current_size + len(line) + 1 > max_chars:
            pieces.append("\n".join(current))
            current = []
            current_size = 0
        if len(line) > max_chars:
            if current:
                pieces.append("\n".join(current))
                current = []
                current_size = 0
            for start in range(0, len(line), max_chars):
                pieces.append(line[start : start + max_chars])
            continue
        current.append(line)
        current_size += len(line) + (1 if len(current) > 1 else 0)

    if current:
        pieces.append("\n".join(current))
    return [piece for piece in pieces if piece.strip()]


def learn_paths(
    store: MemoryStore,
    paths: Iterable[Path],
    extensions: set[str],
    *,
    chunk_size: int = 1200,
    max_bytes: int = 256_000,
    replace: bool = False,
) -> TrainingReport:
    """Import readable text files as durable knowledge memories."""
    if replace:
        store.delete_by_category_prefix("knowledge:")

    files_learned = 0
    files_skipped = 0
    chunks_added = 0

    for path in iter_text_files(paths, extensions):
        try:
            if path.stat().st_size > max_bytes:
                files_skipped += 1
                continue
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            files_skipped += 1
            continue

        chunks = chunk_text(content, max_chars=max(chunk_size, 200))
        if not chunks:
            files_skipped += 1
            continue

        category = f"knowledge:{path.as_posix()}"
        for chunk in chunks:
            store.remember(chunk, category)
            chunks_added += 1
        files_learned += 1

    return TrainingReport(
        files_learned=files_learned,
        chunks_added=chunks_added,
        files_skipped=files_skipped,
    )
