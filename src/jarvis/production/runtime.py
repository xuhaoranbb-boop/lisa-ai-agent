from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from jarvis.config import Settings
from jarvis.production.backup import BackupManager
from jarvis.production.config_manager import ConfigManager
from jarvis.production.crash_recovery import CrashRecovery, RecoveryReport
from jarvis.production.environment import RuntimeEnvironment, resolve_runtime_paths
from jarvis.production.launcher import JarvisLauncher
from jarvis.production.log_manager import LogManager
from jarvis.production.migrations import MigrationManager, MigrationReport
from jarvis.production.permissions import PermissionCenter
from jarvis.production.secrets import SecretManager
from jarvis.production.service_manager import ServiceManager
from jarvis.production.updates import UpdateManager
from jarvis.version import version_info


class ProductionRuntime:
    """Compose production concerns without coupling them into agent logic."""

    def __init__(self, settings: Settings) -> None:
        dotenv_environment = {
            key: value
            for key, value in dotenv_values(Path.cwd() / ".env").items()
            if value is not None
        }
        environment = {**dotenv_environment, **os.environ}
        self.requested_environment = RuntimeEnvironment.from_value(settings.environment)
        default_legacy_data = (Path.home() / ".jarvis").resolve()
        configured_data_dir = (
            None
            if "JARVIS_DATA_DIR" not in os.environ
            and settings.data_dir.resolve() == default_legacy_data
            else settings.data_dir
        )
        self.paths = resolve_runtime_paths(
            environment=self.requested_environment,
            data_dir=configured_data_dir,
        ).ensure()
        self.backup_manager = BackupManager(self.paths)
        self.pending_restore = self.backup_manager.apply_pending_restore()

        self.secret_manager = SecretManager(
            self.paths.secrets,
            environment=environment,
        )
        self.config_manager = ConfigManager(
            self.paths.config,
            secret_manager=self.secret_manager,
            environment=environment,
        )
        self.config_manager.defaults["environment"] = str(self.paths.environment)
        self.settings = self.config_manager.apply_to_settings(settings)
        self.settings = self.settings.model_copy(
            update={
                "data_dir": self.paths.root,
                "environment": str(self.paths.environment),
            }
        )
        self.paths = resolve_runtime_paths(
            environment=self.paths.environment,
            data_dir=self.settings.data_dir,
        ).ensure()
        self.log_manager = LogManager(
            self.paths.logs,
            level=self.settings.logging_level,
            max_bytes=self.settings.logging_max_bytes,
            retention_days=self.settings.logging_retention_days,
        )
        self.backup_manager = BackupManager(
            self.paths,
            public_settings=self.config_manager.public_settings(),
        )
        self.crash_recovery = CrashRecovery(self.paths.runtime / "crash-state.json")
        self.recovery_report = self.crash_recovery.begin_startup(
            ["config", "database", "llm", "asr", "tts", "mcp", "ui"]
        )
        self.permission_center = PermissionCenter(
            dict(self.config_manager.get("permissions", {}) or {}),
            on_change=self._persist_permissions,
        )
        self.service_manager = ServiceManager()
        self.update_manager = UpdateManager(
            self.paths,
            secrets=self.secret_manager,
            backups=self.backup_manager,
        )
        self.launcher = JarvisLauncher()
        self.migration_report: MigrationReport | None = None

    def migrate_database(self) -> MigrationReport:
        database_exists = self.paths.database.exists()

        def pre_migration(_migration: Any) -> None:
            if database_exists:
                self.backup_manager.create(
                    kind="pre-migration",
                    label=str(getattr(_migration, "version", "unknown")),
                )

        report = MigrationManager(self.paths.database).migrate(
            before_migration=pre_migration,
        )
        self.migration_report = report
        self.log_manager.log(
            "INFO",
            "migrations",
            "Database migrations completed.",
            metadata=report.to_dict(),
        )
        return report

    def apply_permission_policy(self, permission_manager: Any) -> None:
        self.permission_center.apply(permission_manager)

    def attach_unfinished_tasks(self, tasks: list[dict[str, Any]]) -> RecoveryReport:
        self.recovery_report = self.crash_recovery.attach_unfinished_tasks(tasks)
        return self.recovery_report

    def create_backup(self, *, kind: str = "manual", label: str = "") -> dict[str, Any]:
        return self.backup_manager.create(kind=kind, label=label).to_dict()

    def restore_backup(self, archive: Path, *, confirmed: bool = False) -> dict[str, Any]:
        return self.backup_manager.stage_restore(archive, confirmed=confirmed)

    def shutdown(self, *, clean: bool = True) -> None:
        self.crash_recovery.mark_shutdown(clean=clean)
        self.launcher.mark_stopped()
        self.log_manager.log(
            "INFO",
            "runtime",
            "JARVIS runtime stopped.",
            metadata={"clean": clean},
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            **version_info(),
            "environment": str(self.paths.environment),
            "paths": self.paths.to_dict(),
            "config": self.config_manager.public_settings(),
            "secrets": self.secret_manager.status(),
            "migration": self.migration_report.to_dict() if self.migration_report else None,
            "recovery": self.recovery_report.to_dict(),
            "services": self.service_manager.snapshot(),
            "launcher": self.launcher.status.to_dict(),
            "update": self.update_manager.snapshot(),
            "logs": self.log_manager.stats(),
            "pending_restore": self.pending_restore,
        }

    def _persist_permissions(self, modes: dict[str, str]) -> None:
        self.config_manager.update({"permissions": modes})
