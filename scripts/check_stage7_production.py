from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from jarvis.config import Settings
from jarvis.production import ProductionRuntime, RuntimeEnvironment
from jarvis.production.backup import BackupManager
from jarvis.production.crash_recovery import CrashRecovery
from jarvis.production.environment import resolve_runtime_paths
from jarvis.production.permissions import PermissionCenter, PermissionMode
from jarvis.production.service_manager import ServiceManager
from jarvis.runtime.contracts import PermissionLevel
from jarvis.runtime.health import ComponentHealth, HealthStatus
from jarvis.runtime.permissions import PermissionManager, PermissionPolicy


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="jarvis-stage7-") as temporary:
        root = Path(temporary)
        resolve_runtime_paths(
            environment=RuntimeEnvironment.TEST,
            root=root,
        ).ensure()
        settings = Settings(
            data_dir=root,
            environment="test",
            model="stage7-test-model",
        )
        runtime = ProductionRuntime(settings)
        migration = runtime.migrate_database()

        backup = BackupManager(
            runtime.paths,
            public_settings=runtime.config_manager.public_settings(),
        )
        backup_record = backup.create(kind="stage7-check")
        backup_verified = backup.verify(backup_record.path)

        recovery_path = runtime.paths.runtime / "crash-check.json"
        first = CrashRecovery(recovery_path)
        first.begin_startup(["bridge", "agent"])
        second = CrashRecovery(recovery_path)
        recovery = second.begin_startup(["bridge", "agent"])
        second.attach_unfinished_tasks([{"id": "check-task", "goal": "verify"}])

        services = ServiceManager()

        async def service_health() -> ComponentHealth:
            return ComponentHealth(name="bridge", status=HealthStatus.HEALTHY)

        async def no_op() -> None:
            return None

        services.register("bridge", health=service_health, start=no_op, stop=no_op)
        await services.start_all()
        service_health_result = await services.health_check("bridge")

        permission_manager = PermissionManager(
            PermissionPolicy(auto_approve={PermissionLevel.READ_ONLY})
        )
        permissions = PermissionCenter({"desktop": PermissionMode.ASK_EVERY_TIME})
        permissions.apply(permission_manager)
        permission_decision = await permission_manager.decide(
            PermissionLevel.READ_ONLY,
            "desktop.screenshot",
            metadata={"category": "desktop"},
        )

        report = {
            "ok": (
                migration.to_version == 2
                and backup_verified["ok"]
                and recovery.unclean_shutdown
                and service_health_result["state"] == "RUNNING"
                and permission_decision.requires_confirmation
            ),
            "environment": str(runtime.paths.environment),
            "database_version": migration.to_version,
            "backup_verified": backup_verified["ok"],
            "crash_detected": recovery.unclean_shutdown,
            "requires_user_decision": recovery.to_dict()["requires_user_decision"],
            "service_state": service_health_result["state"],
            "permission_requires_confirmation": permission_decision.requires_confirmation,
            "version": runtime.snapshot()["version"],
        }
        runtime.shutdown()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
