from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class RuntimeEnvironment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"

    @classmethod
    def from_value(cls, value: str | None) -> RuntimeEnvironment:
        normalized = (value or "development").strip().lower()
        aliases = {
            "dev": cls.DEVELOPMENT,
            "development": cls.DEVELOPMENT,
            "test": cls.TEST,
            "testing": cls.TEST,
            "prod": cls.PRODUCTION,
            "production": cls.PRODUCTION,
        }
        return aliases.get(normalized, cls.DEVELOPMENT)


@dataclass(frozen=True)
class RuntimePaths:
    environment: RuntimeEnvironment
    root: Path
    data: Path
    database: Path
    memory: Path
    logs: Path
    cache: Path
    backups: Path
    runtime: Path
    config: Path
    secrets: Path
    updates: Path
    exports: Path
    plugins: Path

    def ensure(self) -> RuntimePaths:
        for path in (
            self.root,
            self.data,
            self.memory,
            self.logs,
            self.cache,
            self.backups,
            self.runtime,
            self.updates,
            self.exports,
            self.plugins,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        return self

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "environment": str(self.environment),
            "root": str(self.root),
            "data": str(self.data),
            "database": str(self.database),
            "logs": str(self.logs),
            "cache": str(self.cache),
            "backups": str(self.backups),
            "runtime": str(self.runtime),
            "config": str(self.config),
            "test_isolated": self.environment == RuntimeEnvironment.TEST,
        }


def resolve_runtime_paths(
    *,
    environment: str | RuntimeEnvironment | None = None,
    data_dir: str | Path | None = None,
    config_path: str | Path | None = None,
    root: str | Path | None = None,
) -> RuntimePaths:
    runtime_environment = (
        environment
        if isinstance(environment, RuntimeEnvironment)
        else RuntimeEnvironment.from_value(
            environment or os.environ.get("JARVIS_ENVIRONMENT")
        )
    )
    explicit_root = _expanded(root)
    explicit_data = _expanded(data_dir) or _expanded(os.environ.get("JARVIS_DATA_DIR"))

    if explicit_root is not None:
        runtime_root = explicit_root
    elif explicit_data is not None:
        runtime_root = explicit_data
    elif runtime_environment == RuntimeEnvironment.TEST:
        runtime_root = Path(tempfile.gettempdir()) / "jarvis-test-runtime"
    elif runtime_environment == RuntimeEnvironment.PRODUCTION:
        runtime_root = Path.home() / ".jarvis"
    else:
        runtime_root = Path.home() / ".jarvis-dev"

    configured_path = _expanded(config_path) or _expanded(
        os.environ.get("JARVIS_CONFIG_PATH")
    )
    data_root = explicit_data or runtime_root / "data"
    database = runtime_root / "jarvis.db"
    if explicit_data is not None and explicit_data.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        database = explicit_data
        data_root = explicit_data.parent

    return RuntimePaths(
        environment=runtime_environment,
        root=runtime_root.resolve(),
        data=data_root.resolve(),
        database=database.resolve(),
        memory=(runtime_root / "memory").resolve(),
        logs=(runtime_root / "logs").resolve(),
        cache=(runtime_root / "cache").resolve(),
        backups=(runtime_root / "backups").resolve(),
        runtime=(runtime_root / "runtime").resolve(),
        config=(configured_path or runtime_root / "config.json").resolve(),
        secrets=(runtime_root / "secrets.vault").resolve(),
        updates=(runtime_root / "updates").resolve(),
        exports=(runtime_root / "exports").resolve(),
        plugins=(runtime_root / "plugins").resolve(),
    )


def _expanded(value: str | Path | None) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    return Path(value).expanduser()
