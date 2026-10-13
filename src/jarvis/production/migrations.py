from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: Callable[[sqlite3.Connection], None]
    down: Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class MigrationReport:
    from_version: int
    to_version: int
    applied: list[str]
    rollback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "applied": self.applied,
            "rollback": self.rollback,
        }


class MigrationManager:
    """Versioned SQLite migrations with explicit transactional rollback."""

    def __init__(
        self,
        database_path: Path,
        migrations: list[Migration] | None = None,
    ) -> None:
        self.database_path = database_path
        self.migrations = sorted(migrations or default_migrations(), key=lambda item: item.version)
        self._validate_registry()
        self._lock = RLock()

    def current_version(self) -> int:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.database_path) as connection:
            if not _table_exists(connection, "schema_migrations"):
                return 0
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
            return int(row[0] if row else 0)

    def pending(self) -> list[Migration]:
        current = self.current_version()
        return [migration for migration in self.migrations if migration.version > current]

    def migrate(
        self,
        *,
        target_version: int | None = None,
        before_migration: Callable[[Migration], None] | None = None,
    ) -> MigrationReport:
        target = target_version or max(
            (migration.version for migration in self.migrations),
            default=0,
        )
        with self._lock:
            from_version = self.current_version()
            applied: list[str] = []
            for migration in self.pending():
                if migration.version > target:
                    break
                if before_migration is not None:
                    before_migration(migration)
                with _connect(self.database_path) as connection:
                    connection.execute("PRAGMA foreign_keys=ON")
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        migration.up(connection)
                        connection.execute(
                            """
                            CREATE TABLE IF NOT EXISTS schema_migrations (
                                version INTEGER PRIMARY KEY,
                                name TEXT NOT NULL,
                                applied_at TEXT NOT NULL
                            )
                            """
                        )
                        connection.execute(
                            """
                            INSERT INTO schema_migrations(version, name, applied_at)
                            VALUES (?, ?, ?)
                            """,
                            (
                                migration.version,
                                migration.name,
                                datetime.now(UTC).isoformat(timespec="seconds"),
                            ),
                        )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                applied.append(f"{migration.version}:{migration.name}")
            return MigrationReport(
                from_version=from_version,
                to_version=self.current_version(),
                applied=applied,
            )

    def rollback(self, *, steps: int = 1) -> MigrationReport:
        steps = max(1, int(steps))
        with self._lock:
            from_version = self.current_version()
            rolled_back: list[str] = []
            for _ in range(steps):
                current = self.current_version()
                migration = next(
                    (
                        item
                        for item in reversed(self.migrations)
                        if item.version == current
                    ),
                    None,
                )
                if migration is None:
                    break
                with _connect(self.database_path) as connection:
                    connection.execute("PRAGMA foreign_keys=ON")
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        migration.down(connection)
                        connection.execute(
                            "DELETE FROM schema_migrations WHERE version = ?",
                            (migration.version,),
                        )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                rolled_back.append(f"{migration.version}:{migration.name}")
            return MigrationReport(
                from_version=from_version,
                to_version=self.current_version(),
                applied=rolled_back,
                rollback=True,
            )

    def _validate_registry(self) -> None:
        versions = [migration.version for migration in self.migrations]
        if len(versions) != len(set(versions)):
            raise ValueError("Migration versions must be unique.")
        if versions and versions != list(range(1, max(versions) + 1)):
            raise ValueError("Migration versions must be contiguous from 1.")


def default_migrations() -> list[Migration]:
    return [
        Migration(
            version=1,
            name="production_metadata",
            up=_migration_1_up,
            down=_migration_1_down,
        ),
        Migration(
            version=2,
            name="runtime_query_indexes",
            up=_migration_2_up,
            down=_migration_2_down,
        ),
    ]


def _migration_1_up(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS production_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def _migration_1_down(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS production_metadata")


def _migration_2_up(connection: sqlite3.Connection) -> None:
    if _has_columns(connection, "tasks", {"status", "updated_at"}):
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_status_updated "
            "ON tasks(status, updated_at DESC)"
        )
    if _has_columns(connection, "executions", {"status", "updated_at"}):
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_executions_status_updated "
            "ON executions(status, updated_at DESC)"
        )
    if _has_columns(connection, "agent_trace", {"event_type", "created_at"}):
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_trace_type_created "
            "ON agent_trace(event_type, created_at DESC)"
        )


def _migration_2_down(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX IF EXISTS idx_tasks_status_updated")
    connection.execute("DROP INDEX IF EXISTS idx_executions_status_updated")
    connection.execute("DROP INDEX IF EXISTS idx_agent_trace_type_created")


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _has_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: set[str],
) -> bool:
    if not _table_exists(connection, table):
        return False
    existing = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    return columns.issubset(existing)


@contextmanager
def _connect(path: Path):
    connection = sqlite3.connect(path)
    try:
        yield connection
    finally:
        connection.close()
