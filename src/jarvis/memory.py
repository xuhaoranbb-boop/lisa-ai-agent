from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

_SENSITIVE_MEMORY_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|secret|password|passwd|pwd)\b"
        r"\s*[:=]\s*\S+",
        re.I,
    ),
    re.compile(r"\b(?:bearer\s+)[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?:密码|口令|密钥|令牌|验证码)\s*[:：=]\s*\S+"),
)


def is_sensitive_memory(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SENSITIVE_MEMORY_PATTERNS)


@dataclass(frozen=True)
class Memory:
    id: int
    content: str
    category: str
    created_at: str
    updated_at: str
    importance: int = 3
    expires_at: str | None = None
    source: str = "conversation"
    task_id: str | None = None
    confidence: float = 1.0
    last_used_at: str | None = None
    summary: str = ""
    metadata: dict[str, object] | None = None
    session_id: str | None = None
    owner_id: str = "local"


@dataclass(frozen=True)
class Message:
    id: int
    session_id: str
    role: str
    content: str
    created_at: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class MemoryStore:
    """SQLite-backed conversation history and searchable long-term memory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._fts_enabled = False
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    importance INTEGER NOT NULL DEFAULT 3,
                    expires_at TEXT,
                    source TEXT NOT NULL DEFAULT 'conversation',
                    task_id TEXT,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    last_used_at TEXT,
                    summary TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    session_id TEXT,
                    owner_id TEXT NOT NULL DEFAULT 'local'
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session_id
                    ON messages(session_id, id);

                CREATE TABLE IF NOT EXISTS memory_embeddings (
                    memory_id INTEGER PRIMARY KEY,
                    model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    embedding_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS session_summaries (
                    session_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    covered_message_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._migrate_memory_columns()
            try:
                self._connection.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                        content,
                        category,
                        content='memories',
                        content_rowid='id',
                        tokenize='unicode61'
                    );

                    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                        INSERT INTO memories_fts(rowid, content, category)
                        VALUES (new.id, new.content, new.category);
                    END;

                    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, content, category)
                        VALUES ('delete', old.id, old.content, old.category);
                    END;

                    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, content, category)
                        VALUES ('delete', old.id, old.content, old.category);
                        INSERT INTO memories_fts(rowid, content, category)
                        VALUES (new.id, new.content, new.category);
                    END;
                    """
                )
                self._fts_enabled = True
            except sqlite3.OperationalError:
                self._fts_enabled = False

    def _migrate_memory_columns(self) -> None:
        columns = {
            "importance": "INTEGER NOT NULL DEFAULT 3",
            "expires_at": "TEXT",
            "source": "TEXT NOT NULL DEFAULT 'conversation'",
            "task_id": "TEXT",
            "confidence": "REAL NOT NULL DEFAULT 1.0",
            "last_used_at": "TEXT",
            "summary": "TEXT NOT NULL DEFAULT ''",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
            "session_id": "TEXT",
            "owner_id": "TEXT NOT NULL DEFAULT 'local'",
        }
        existing = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(memories)").fetchall()
        }
        for name, definition in columns.items():
            if name in existing:
                continue
            self._connection.execute(
                f"ALTER TABLE memories ADD COLUMN {name} {definition}"
            )

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[\w\u4e00-\u9fff]+", query, flags=re.UNICODE)
        return " OR ".join(f'"{token}"' for token in tokens[:12])

    def remember(
        self,
        content: str,
        category: str = "general",
        *,
        importance: int = 3,
        expires_at: str | None = None,
        source: str = "conversation",
        task_id: str | None = None,
        confidence: float = 1.0,
        summary: str = "",
        metadata: dict[str, object] | None = None,
        session_id: str | None = None,
        owner_id: str = "local",
    ) -> Memory:
        content = content.strip()
        category = category.strip() or "general"
        if not content:
            raise ValueError("Memory content cannot be empty.")
        if is_sensitive_memory(content):
            raise ValueError("Sensitive credentials cannot be stored in long-term memory.")
        importance = max(1, min(int(importance), 5))
        confidence = max(0.0, min(float(confidence), 1.0))
        source = source.strip() or "conversation"
        summary = summary.strip()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, default=str)
        owner_id = owner_id.strip() or "local"

        now = utc_now()
        with self._lock, self._connection:
            existing = self._connection.execute(
                """
                SELECT *
                FROM memories
                WHERE content = ? AND category = ? AND owner_id = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (content, category, owner_id),
            ).fetchone()
            if existing is not None:
                self._connection.execute(
                    """
                    UPDATE memories
                    SET updated_at = ?, importance = ?, expires_at = ?, source = ?,
                        task_id = ?, confidence = ?, summary = ?, metadata_json = ?,
                        session_id = ?, owner_id = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        importance,
                        expires_at,
                        source,
                        task_id,
                        confidence,
                        summary,
                        metadata_json,
                        session_id,
                        owner_id,
                        int(existing["id"]),
                    ),
                )
                row = self._connection.execute(
                    "SELECT * FROM memories WHERE id = ?", (int(existing["id"]),)
                ).fetchone()
                return self._row_to_memory(row)

            cursor = self._connection.execute(
                """
                INSERT INTO memories(
                    content, category, created_at, updated_at,
                    importance, expires_at, source, task_id, confidence,
                    summary, metadata_json, session_id, owner_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content,
                    category,
                    now,
                    now,
                    importance,
                    expires_at,
                    source,
                    task_id,
                    confidence,
                    summary,
                    metadata_json,
                    session_id,
                    owner_id,
                ),
            )
            memory_id = int(cursor.lastrowid)
            row = self._connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        return self._row_to_memory(row)

    def recall(
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

        with self._lock:
            now = utc_now()
            scope_sql, scope_params = self._memory_scope(owner_id, session_id)
            if self._fts_enabled:
                fts_query = self._fts_query(query)
                if fts_query:
                    try:
                        rows = self._connection.execute(
                            """
                            SELECT m.*
                            FROM memories_fts f
                            JOIN memories m ON m.id = f.rowid
                            WHERE memories_fts MATCH ?
                              AND (m.expires_at IS NULL OR m.expires_at > ?)
                              {scope_sql}
                            ORDER BY bm25(memories_fts), m.importance DESC, m.updated_at DESC
                            LIMIT ?
                            """,
                            (fts_query, now, *scope_params, limit),
                        ).fetchall()
                        if rows:
                            memories = [self._row_to_memory(row) for row in rows]
                            self._touch_memories([memory.id for memory in memories])
                            return memories
                    except sqlite3.OperationalError:
                        pass

            pattern = f"%{query}%"
            rows = self._connection.execute(
                f"""
                SELECT m.*
                FROM memories m
                WHERE (m.content LIKE ? OR m.category LIKE ?)
                  AND (m.expires_at IS NULL OR m.expires_at > ?)
                  {scope_sql}
                ORDER BY m.importance DESC, m.updated_at DESC
                LIMIT ?
                """,
                (pattern, pattern, now, *scope_params, limit),
            ).fetchall()
        memories = [self._row_to_memory(row) for row in rows]
        self._touch_memories([memory.id for memory in memories])
        return memories

    def list_memories(
        self,
        limit: int = 50,
        *,
        include_expired: bool = False,
        owner_id: str | None = None,
        session_id: str | None = None,
    ) -> list[Memory]:
        now = utc_now()
        clauses: list[str] = []
        params: list[object] = []
        if not include_expired:
            clauses.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(now)
        scope_sql, scope_params = self._memory_scope(owner_id, session_id)
        if scope_sql:
            clauses.append(scope_sql.removeprefix("AND ").strip())
            params.extend(scope_params)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT m.*
                FROM memories m
                {where}
                ORDER BY m.importance DESC, m.updated_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._row_to_memory(row) for row in rows]

    def recall_for_context(
        self,
        query: str,
        limit: int = 6,
        *,
        owner_id: str | None = None,
        session_id: str | None = None,
    ) -> list[Memory]:
        """Return search matches, then fill remaining slots with recent memories."""
        results = self.recall(
            query,
            limit,
            owner_id=owner_id,
            session_id=session_id,
        )
        if len(results) >= limit:
            return results

        seen = {memory.id for memory in results}
        for memory in self.list_memories(
            limit,
            owner_id=owner_id,
            session_id=session_id,
        ):
            if memory.id not in seen:
                results.append(memory)
                seen.add(memory.id)
            if len(results) >= limit:
                break
        return results

    def get_memory(self, memory_id: int) -> Memory | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
        return self._row_to_memory(row) if row else None

    def set_embedding(
        self,
        memory_id: int,
        model: str,
        embedding: list[float],
    ) -> None:
        now = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO memory_embeddings(
                    memory_id, model, dimensions, embedding_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    model = excluded.model,
                    dimensions = excluded.dimensions,
                    embedding_json = excluded.embedding_json,
                    updated_at = excluded.updated_at
                """,
                (
                    memory_id,
                    model,
                    len(embedding),
                    json.dumps(embedding),
                    now,
                ),
            )

    def list_embeddings(
        self,
        *,
        owner_id: str | None = None,
        session_id: str | None = None,
    ) -> list[tuple[Memory, list[float]]]:
        scope_sql, scope_params = self._memory_scope(owner_id, session_id)
        now = utc_now()
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT m.*, e.embedding_json
                FROM memories m
                JOIN memory_embeddings e ON e.memory_id = m.id
                WHERE (m.expires_at IS NULL OR m.expires_at > ?)
                  {scope_sql}
                """,
                (now, *scope_params),
            ).fetchall()
        results: list[tuple[Memory, list[float]]] = []
        for row in rows:
            try:
                embedding = json.loads(str(row["embedding_json"]))
            except (TypeError, ValueError):
                continue
            if isinstance(embedding, list):
                results.append(
                    (
                        self._row_to_memory(row),
                        [float(value) for value in embedding],
                    )
                )
        return results

    def save_session_summary(
        self,
        session_id: str,
        summary: str,
        covered_message_id: int,
    ) -> None:
        summary = summary.strip()
        if not session_id or not summary:
            return
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO session_summaries(
                    session_id, summary, covered_message_id, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    summary = excluded.summary,
                    covered_message_id = excluded.covered_message_id,
                    updated_at = excluded.updated_at
                """,
                (session_id, summary, covered_message_id, utc_now()),
            )

    def get_session_summary(
        self,
        session_id: str,
    ) -> tuple[str, int] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT summary, covered_message_id
                FROM session_summaries
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row["summary"]), int(row["covered_message_id"])

    def forget(self, memory_id: int) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return cursor.rowcount > 0

    def update_memory(
        self,
        memory_id: int,
        *,
        content: str | None = None,
        category: str | None = None,
        importance: int | None = None,
        expires_at: str | None = None,
        source: str | None = None,
        task_id: str | None = None,
        confidence: float | None = None,
        summary: str | None = None,
        metadata: dict[str, object] | None = None,
        session_id: str | None = None,
        owner_id: str | None = None,
    ) -> Memory | None:
        updates: list[str] = ["updated_at = ?"]
        values: list[object] = [utc_now()]
        if content is not None:
            content = content.strip()
            if not content:
                raise ValueError("Memory content cannot be empty.")
            if is_sensitive_memory(content):
                raise ValueError(
                    "Sensitive credentials cannot be stored in long-term memory."
                )
            updates.append("content = ?")
            values.append(content)
        if category is not None:
            updates.append("category = ?")
            values.append(category.strip() or "general")
        if importance is not None:
            updates.append("importance = ?")
            values.append(max(1, min(int(importance), 5)))
        if expires_at is not None:
            updates.append("expires_at = ?")
            values.append(expires_at)
        if source is not None:
            updates.append("source = ?")
            values.append(source.strip() or "conversation")
        if task_id is not None:
            updates.append("task_id = ?")
            values.append(task_id)
        if confidence is not None:
            updates.append("confidence = ?")
            values.append(max(0.0, min(float(confidence), 1.0)))
        if summary is not None:
            updates.append("summary = ?")
            values.append(summary.strip())
        if metadata is not None:
            updates.append("metadata_json = ?")
            values.append(json.dumps(metadata, ensure_ascii=False, default=str))
        if session_id is not None:
            updates.append("session_id = ?")
            values.append(session_id)
        if owner_id is not None:
            updates.append("owner_id = ?")
            values.append(owner_id.strip() or "local")

        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"UPDATE memories SET {', '.join(updates)} WHERE id = ?",
                (*values, memory_id),
            )
            if cursor.rowcount == 0:
                return None
            row = self._connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        return self._row_to_memory(row)

    def expire_memories(self) -> int:
        now = utc_now()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            )
        return cursor.rowcount

    def clear_memories(self) -> int:
        with self._lock, self._connection:
            count = int(
                self._connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            )
            self._connection.execute("DELETE FROM memories")
        return count

    def delete_by_category_prefix(self, prefix: str) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM memories WHERE category LIKE ?",
                (f"{prefix}%",),
            )
        return cursor.rowcount

    def remember_task(
        self,
        task_id: str,
        content: str,
        *,
        importance: int = 4,
        session_id: str | None = None,
        owner_id: str = "local",
    ) -> Memory:
        """Upsert one durable summary row per task."""
        content = content.strip()
        if not content:
            raise ValueError("Task memory content cannot be empty.")
        if is_sensitive_memory(content):
            content = "Task details contained sensitive data and were not stored."
        importance = max(1, min(int(importance), 5))
        now = utc_now()
        with self._lock, self._connection:
            existing = self._connection.execute(
                """
                SELECT *
                FROM memories
                WHERE task_id = ? AND category = 'task'
                ORDER BY id ASC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if existing is not None:
                self._connection.execute(
                    """
                    UPDATE memories
                    SET content = ?, importance = ?, expires_at = NULL,
                        source = 'task', updated_at = ?, session_id = ?,
                        owner_id = ?
                    WHERE id = ?
                    """,
                    (
                        content,
                        importance,
                        now,
                        session_id,
                        owner_id,
                        int(existing["id"]),
                    ),
                )
                row = self._connection.execute(
                    "SELECT * FROM memories WHERE id = ?",
                    (int(existing["id"]),),
                ).fetchone()
                return self._row_to_memory(row)

            cursor = self._connection.execute(
                """
                INSERT INTO memories(
                    content, category, created_at, updated_at,
                    importance, expires_at, source, task_id, session_id, owner_id
                )
                VALUES (?, 'task', ?, ?, ?, NULL, 'task', ?, ?, ?)
                """,
                (content, now, now, importance, task_id, session_id, owner_id),
            )
            row = self._connection.execute(
                "SELECT * FROM memories WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
        return self._row_to_memory(row)

    def add_message(self, session_id: str, role: str, content: str) -> Message:
        now = utc_now()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO messages(session_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, role, content, now),
            )
            message_id = int(cursor.lastrowid)
            row = self._connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return self._row_to_message(row)

    def history(self, session_id: str, limit: int = 24) -> list[Message]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT *
                FROM (
                    SELECT *
                    FROM messages
                    WHERE session_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (session_id, limit),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def list_sessions(
        self,
        limit: int = 100,
        *,
        search: str = "",
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        params: list[object] = []
        if search.strip():
            clauses.append("(session_id LIKE ? OR content LIKE ?)")
            pattern = f"%{search.strip()}%"
            params.extend([pattern, pattern])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT
                    session_id,
                    COUNT(*) AS message_count,
                    MAX(created_at) AS updated_at,
                    MAX(id) AS last_message_id
                FROM messages
                {where}
                GROUP BY session_id
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (*params, max(1, min(int(limit), 1_000))),
            ).fetchall()
        return [
            {
                "session_id": str(row["session_id"]),
                "message_count": int(row["message_count"]),
                "updated_at": str(row["updated_at"]),
                "last_message_id": int(row["last_message_id"]),
            }
            for row in rows
        ]

    def history_after(
        self,
        session_id: str,
        message_id: int,
        limit: int = 24,
    ) -> list[Message]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT *
                FROM messages
                WHERE session_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (session_id, message_id, limit),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def _touch_memories(self, memory_ids: Iterable[int]) -> None:
        ids = list(dict.fromkeys(int(memory_id) for memory_id in memory_ids))
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._connection:
            self._connection.execute(
                f"UPDATE memories SET last_used_at = ? WHERE id IN ({placeholders})",
                (utc_now(), *ids),
            )

    def mark_used(self, memory_ids: Iterable[int]) -> None:
        self._touch_memories(memory_ids)

    @staticmethod
    def _memory_scope(
        owner_id: str | None,
        session_id: str | None,
    ) -> tuple[str, tuple[object, ...]]:
        clauses: list[str] = []
        params: list[object] = []
        if owner_id:
            clauses.append("m.owner_id = ?")
            params.append(owner_id)
        if session_id:
            clauses.append("(m.session_id IS NULL OR m.session_id = ?)")
            params.append(session_id)
        if not clauses:
            return "", ()
        return "AND " + " AND ".join(clauses), tuple(params)

    def clear_history(self, session_id: str) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
        return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> Memory:
        return Memory(
            id=int(row["id"]),
            content=str(row["content"]),
            category=str(row["category"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            importance=int(row["importance"]),
            expires_at=str(row["expires_at"]) if row["expires_at"] is not None else None,
            source=str(row["source"]),
            task_id=str(row["task_id"]) if row["task_id"] is not None else None,
            confidence=float(row["confidence"]) if row["confidence"] is not None else 1.0,
            last_used_at=(
                str(row["last_used_at"])
                if row["last_used_at"] is not None
                else None
            ),
            summary=str(row["summary"] or ""),
            metadata=json.loads(str(row["metadata_json"] or "{}")),
            session_id=str(row["session_id"]) if row["session_id"] is not None else None,
            owner_id=str(row["owner_id"] or "local"),
        )

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Message:
        return Message(
            id=int(row["id"]),
            session_id=str(row["session_id"]),
            role=str(row["role"]),
            content=str(row["content"]),
            created_at=str(row["created_at"]),
        )


def memory_lines(memories: Iterable[Memory]) -> list[str]:
    return [
        (
            f"[{memory.category}; importance={memory.importance}; "
            f"confidence={memory.confidence:.2f}] {memory.content}"
        )
        for memory in memories
    ]
