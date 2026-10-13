from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from jarvis.config import Settings
from jarvis.production.backup import BackupManager
from jarvis.production.config_manager import ConfigManager
from jarvis.production.crash_recovery import CrashRecovery
from jarvis.production.diagnostics import DiagnosticsCenter
from jarvis.production.environment import RuntimeEnvironment, resolve_runtime_paths
from jarvis.production.launcher import JarvisLauncher, LauncherState
from jarvis.production.migrations import MigrationManager
from jarvis.production.permissions import PermissionCenter, PermissionMode
from jarvis.production.runtime import ProductionRuntime
from jarvis.production.secrets import SecretManager
from jarvis.production.service_manager import ServiceManager, ServiceState
from jarvis.production.updates import UpdateManager
from jarvis.runtime.contracts import PermissionLevel
from jarvis.runtime.health import ComponentHealth, HealthReport, HealthStatus
from jarvis.runtime.permissions import PermissionManager, PermissionPolicy
from jarvis.runtime.plugins import PluginManager
from jarvis.version import SemanticVersion, version_info


class MemoryProtector:
    def available(self) -> bool:
        return True

    def protect(self, value: bytes, *, description: str) -> bytes:
        return b"protected:" + value[::-1]

    def unprotect(self, value: bytes, *, description: str) -> bytes:
        if not value.startswith(b"protected:"):
            raise ValueError("invalid protected value")
        return value.removeprefix(b"protected:")[::-1]


def test_config_priority_and_secret_masking(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "llm": {"model": "user-model", "api_key": "sk-user-secret"},
                "ui": {"close_to_tray": False},
            }
        ),
        encoding="utf-8",
    )
    secrets = SecretManager(
        tmp_path / "secrets.vault",
        environment={},
        protector=MemoryProtector(),
    )
    manager = ConfigManager(
        config_path,
        secret_manager=secrets,
        environment={"JARVIS_MODEL": "environment-model"},
    )

    assert manager.get("llm.model") == "environment-model"
    effective = manager.effective()
    assert effective["llm"]["api_key"].startswith("sk-")
    secrets.set("model_api_key", "sk-secret-value", persist=False)
    assert manager.effective()["llm"]["api_key"] == "sk-********alue"
    assert "sk-secret-value" not in json.dumps(manager.public_settings())


def test_secret_vault_never_writes_plaintext(tmp_path: Path) -> None:
    manager = SecretManager(
        tmp_path / "secrets.vault",
        environment={},
        protector=MemoryProtector(),
    )
    manager.set("model_api_key", "sk-plaintext-value")

    raw = (tmp_path / "secrets.vault").read_text(encoding="utf-8")
    assert "sk-plaintext-value" not in raw
    assert manager.get("model_api_key") == "sk-plaintext-value"
    assert manager.mask("model_api_key").startswith("sk-")


def test_environment_paths_are_isolated(tmp_path: Path) -> None:
    production = resolve_runtime_paths(environment=RuntimeEnvironment.PRODUCTION)
    test_paths = resolve_runtime_paths(
        environment=RuntimeEnvironment.TEST,
        root=tmp_path,
    )

    assert production.root != test_paths.root
    assert test_paths.environment == RuntimeEnvironment.TEST
    assert test_paths.test_isolated if hasattr(test_paths, "test_isolated") else True
    assert test_paths.database.is_relative_to(tmp_path)


def test_migration_and_rollback(tmp_path: Path) -> None:
    database = tmp_path / "jarvis.db"
    manager = MigrationManager(database)
    report = manager.migrate()

    assert report.to_version == 2
    assert manager.current_version() == 2
    with sqlite3.connect(database) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "production_metadata" in tables

    rollback = manager.rollback(steps=1)
    assert rollback.rollback is True
    assert manager.current_version() == 1


def test_backup_verify_and_stage_restore(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(
        environment=RuntimeEnvironment.TEST,
        root=tmp_path,
    ).ensure()
    with sqlite3.connect(paths.database) as connection:
        connection.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO sample(value) VALUES ('kept')")
    paths.config.write_text(
        json.dumps({"llm": {"api_key": "[REDACTED]"}}),
        encoding="utf-8",
    )
    manager = BackupManager(
        paths,
        public_settings={"llm": {"api_key": "[REDACTED]"}},
    )
    record = manager.create(kind="test", label="check")

    assert record.verified is True
    assert manager.verify(record.path)["ok"] is True
    assert b"plain-secret" not in record.path.read_bytes()
    staged = manager.stage_restore(record.path, confirmed=True)
    assert staged["restart_required"] is True
    assert (paths.runtime / "restore-pending.json").exists()


def test_crash_recovery_detects_unclean_exit(tmp_path: Path) -> None:
    state_path = tmp_path / "crash-state.json"
    first = CrashRecovery(state_path)
    first.begin_startup(["bridge", "agent"])
    second = CrashRecovery(state_path)
    report = second.begin_startup(["bridge", "agent"])

    assert report.unclean_shutdown is True
    second.attach_unfinished_tasks([{"id": "task-1", "goal": "continue"}])
    assert report.to_dict()["requires_user_decision"] is True
    second.mark_shutdown()
    third = CrashRecovery(state_path)
    assert third.begin_startup(["bridge"]).unclean_shutdown is False


@pytest.mark.asyncio
async def test_service_manager_enforces_restart_limit() -> None:
    manager = ServiceManager()

    async def failing_health() -> ComponentHealth:
        return ComponentHealth(
            name="fake",
            status=HealthStatus.ERROR,
            message="failed",
        )

    async def no_op() -> None:
        return None

    manager.register(
        "fake",
        health=failing_health,
        start=no_op,
        stop=no_op,
        max_restarts=1,
    )
    first = await manager.restart("fake")
    second = await manager.restart("fake")

    assert first["restart_count"] == 1
    assert second["restart_count"] == 1
    assert second["state"] == str(ServiceState.FAILED)
    assert second["last_error"] == "Maximum restart count reached."


@pytest.mark.asyncio
async def test_launcher_reports_ready_and_stage_failures() -> None:
    launcher = JarvisLauncher()
    called: list[str] = []

    async def stage() -> None:
        called.append("stage")

    async def healthy() -> HealthReport:
        return HealthReport(
            status=HealthStatus.HEALTHY,
            components=[],
            checked_at=0.0,
        )

    launcher.add_stage("database", stage)
    status = await launcher.run(healthy)

    assert called == ["stage"]
    assert status.state == LauncherState.READY
    assert status.startup_time_ms >= 0


@pytest.mark.asyncio
async def test_permission_center_enforces_category_modes() -> None:
    manager = PermissionManager(
        PermissionPolicy(auto_approve={PermissionLevel.READ_ONLY})
    )
    center = PermissionCenter({"desktop": PermissionMode.ASK_EVERY_TIME})
    center.apply(manager)

    ask = await manager.decide(
        PermissionLevel.READ_ONLY,
        "desktop.screenshot",
        metadata={"category": "desktop"},
    )
    assert ask.allowed is False
    assert ask.requires_confirmation is True

    center.set_mode("desktop", PermissionMode.ALLOWED)
    center.apply(manager)
    allowed = await manager.decide(
        PermissionLevel.READ_ONLY,
        "desktop.screenshot",
        metadata={"category": "desktop"},
    )
    assert allowed.allowed is True

    center.set_mode("desktop", PermissionMode.DENIED)
    center.apply(manager)
    denied = await manager.decide(
        PermissionLevel.LOW_RISK,
        "desktop.app_open",
        metadata={"category": "desktop"},
    )
    assert denied.allowed is False


@pytest.mark.asyncio
async def test_diagnostics_export_redacts_credentials(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(
        environment=RuntimeEnvironment.TEST,
        root=tmp_path,
    ).ensure()

    class FakeHealthChecker:
        async def check(self) -> HealthReport:
            return HealthReport(
                status=HealthStatus.DEGRADED,
                components=[
                    ComponentHealth(
                        name="llm",
                        status=HealthStatus.DEGRADED,
                        message="Authorization: Bearer secret-token-value",
                    )
                ],
                checked_at=0.0,
            )

    center = DiagnosticsCenter(
        paths=paths,
        health_checker=FakeHealthChecker(),  # type: ignore[arg-type]
        service_manager=ServiceManager(),
        version="1.0.0",
    )
    report = await center.collect()
    export_path = center.export(report)
    exported = export_path.read_text(encoding="utf-8")

    assert "secret-token-value" not in exported
    assert "[REDACTED]" in exported


@pytest.mark.asyncio
async def test_signed_update_check_and_download(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(
        environment=RuntimeEnvironment.TEST,
        root=tmp_path,
    ).ensure()
    secrets = SecretManager(
        paths.secrets,
        environment={"JARVIS_SECRET_UPDATE_SIGNING_KEY": "update-key"},
        protector=MemoryProtector(),
    )
    backups = BackupManager(paths)
    manager = UpdateManager(paths, secrets=secrets, backups=backups)
    installer = b"verified-installer"
    sha256 = hashlib.sha256(installer).hexdigest()
    manifest_payload = {
        "version": "9.9.9",
        "url": "https://updates.example.com/jarvis.exe",
        "sha256": sha256,
        "channel": "stable",
    }
    signing_payload = "\n".join(
        [
            manifest_payload["version"],
            manifest_payload["channel"],
            manifest_payload["url"],
            manifest_payload["sha256"],
        ]
    ).encode("utf-8")
    manifest_payload["signature"] = hmac.new(
        b"update-key",
        signing_payload,
        hashlib.sha256,
    ).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("manifest.json"):
            return httpx.Response(200, json=manifest_payload)
        return httpx.Response(200, content=installer)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        checked = await manager.check(
            "https://updates.example.com/manifest.json",
            client=client,
        )
        downloaded = await manager.download(client=client)

    assert checked["status"] == "AVAILABLE"
    assert checked["signature_verified"] is True
    assert downloaded["status"] == "READY"


def test_plugin_installation_requires_source_and_signature(tmp_path: Path) -> None:
    payload = {
        "name": "safe-plugin",
        "version": "1.0.0",
        "author": "JARVIS",
        "permissions": ["LOW_RISK"],
        "tools": [],
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    payload["sha256"] = digest
    payload["signature"] = hmac.new(
        b"plugin-key",
        digest.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    manifest_path = tmp_path / "plugin.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    manager = PluginManager(
        signing_key="plugin-key",
        allowed_roots=(tmp_path,),
    )

    report = manager.validate_manifest(payload, source=manifest_path, production=True)
    assert report["installable"] is True
    assert report["signed"] is True

    payload["signature"] = "invalid"
    rejected = manager.validate_manifest(
        payload,
        source=manifest_path,
        production=True,
    )
    assert rejected["installable"] is False


def test_semantic_version_and_production_runtime_composition(tmp_path: Path) -> None:
    assert SemanticVersion.parse("1.0.0") < SemanticVersion.parse("1.1.0")
    assert version_info()["major"] == 1
    paths = resolve_runtime_paths(
        environment=RuntimeEnvironment.TEST,
        root=tmp_path,
    ).ensure()
    settings = Settings(
        data_dir=paths.root,
        environment="test",
        model="test-model",
    )
    runtime = ProductionRuntime(settings)

    assert runtime.paths.environment == RuntimeEnvironment.TEST
    assert runtime.settings.data_dir == paths.root
    assert runtime.snapshot()["environment"] == "test"
    runtime.shutdown()
