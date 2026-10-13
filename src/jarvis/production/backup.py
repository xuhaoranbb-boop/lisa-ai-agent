from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis.production.environment import RuntimePaths


@dataclass(frozen=True)
class BackupRecord:
    path: Path
    created_at: str
    size_bytes: int
    verified: bool
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "name": self.path.name,
            "created_at": self.created_at,
            "size_bytes": self.size_bytes,
            "verified": self.verified,
            "kind": self.kind,
        }


class BackupManager:
    """Create, verify, stage, and apply database/configuration backups."""

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        public_settings: dict[str, Any] | None = None,
    ) -> None:
        self.paths = paths
        self.public_settings = public_settings or {}

    def create(
        self,
        *,
        kind: str = "manual",
        label: str = "",
    ) -> BackupRecord:
        timestamp = datetime.now(UTC)
        safe_label = _safe_label(label)
        name = (
            f"jarvis-{kind}-{timestamp.strftime('%Y%m%d-%H%M%S')}"
            f"{f'-{safe_label}' if safe_label else ''}.zip"
        )
        destination = self.paths.backups / name
        manifest = {
            "format": 1,
            "kind": kind,
            "created_at": timestamp.isoformat(timespec="seconds"),
            "environment": str(self.paths.environment),
            "contains_secrets": False,
            "files": {},
        }

        with tempfile.TemporaryDirectory(prefix="jarvis-backup-") as temporary:
            workspace = Path(temporary)
            payload_root = workspace / "payload"
            payload_root.mkdir(parents=True, exist_ok=True)

            if self.paths.database.exists():
                database_copy = payload_root / "jarvis.db"
                _sqlite_backup(self.paths.database, database_copy)
                _register_file(manifest, database_copy, payload_root)

            if self.paths.config.exists():
                config_copy = payload_root / "config.json"
                config_copy.write_text(
                    json.dumps(self.public_settings, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                _register_file(manifest, config_copy, payload_root)

            persona = self.paths.root / "persona.json"
            if persona.exists():
                persona_copy = payload_root / "persona.json"
                shutil.copy2(persona, persona_copy)
                _register_file(manifest, persona_copy, payload_root)

            if self.paths.plugins.exists():
                for manifest_path in sorted(self.paths.plugins.glob("*/plugin.json")):
                    relative = Path("plugins") / manifest_path.parent.name / "plugin.json"
                    target = payload_root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(manifest_path, target)
                    _register_file(manifest, target, payload_root)

            manifest_path = workspace / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.paths.backups.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(
                destination,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                archive.write(manifest_path, "manifest.json")
                for file_path in payload_root.rglob("*"):
                    if file_path.is_file():
                        archive.write(
                            file_path,
                            file_path.relative_to(payload_root).as_posix(),
                        )
        verification = self.verify(destination)
        return BackupRecord(
            path=destination,
            created_at=str(manifest["created_at"]),
            size_bytes=destination.stat().st_size,
            verified=bool(verification["ok"]),
            kind=kind,
        )

    def create_if_due(self, *, interval_hours: int = 24) -> BackupRecord | None:
        latest_time = 0.0
        for backup in self.list_backups():
            try:
                latest_time = max(latest_time, backup.path.stat().st_mtime)
            except OSError:
                continue
        if latest_time and (
            datetime.now(UTC).timestamp() - latest_time < interval_hours * 3600
        ):
            return None
        return self.create(kind="automatic", label="daily")

    def list_backups(self) -> list[BackupRecord]:
        if not self.paths.backups.exists():
            return []
        records: list[BackupRecord] = []
        for path in sorted(
            self.paths.backups.glob("*.zip"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            try:
                with zipfile.ZipFile(path) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                records.append(
                    BackupRecord(
                        path=path,
                        created_at=str(manifest.get("created_at") or ""),
                        size_bytes=path.stat().st_size,
                        verified=bool(self.verify(path)["ok"]),
                        kind=str(manifest.get("kind") or "unknown"),
                    )
                )
            except Exception:
                continue
        return records

    def verify(self, archive_path: Path) -> dict[str, Any]:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                if manifest.get("contains_secrets") is not False:
                    return {
                        "ok": False,
                        "error": "Backup manifest does not attest secret-free data.",
                    }
                failures: list[str] = []
                for relative, metadata in dict(manifest.get("files") or {}).items():
                    try:
                        data = archive.read(relative)
                    except KeyError:
                        failures.append(f"missing:{relative}")
                        continue
                    digest = hashlib.sha256(data).hexdigest()
                    if digest != metadata.get("sha256"):
                        failures.append(f"checksum:{relative}")
                    if len(data) != int(metadata.get("size_bytes") or -1):
                        failures.append(f"size:{relative}")
                return {"ok": not failures, "failures": failures, "manifest": manifest}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def stage_restore(self, archive_path: Path, *, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise PermissionError("Backup restore requires explicit confirmation.")
        verification = self.verify(archive_path)
        if not verification["ok"]:
            raise ValueError(f"Backup verification failed: {verification}")
        pending = self.paths.runtime / "restore-pending"
        if pending.exists():
            shutil.rmtree(pending)
        pending.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                target = (pending / member.filename).resolve()
                if pending.resolve() not in target.parents and target != pending.resolve():
                    raise ValueError("Backup contains an unsafe path.")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
        marker = self.paths.runtime / "restore-pending.json"
        marker.write_text(
            json.dumps(
                {
                    "archive": str(archive_path),
                    "staged_at": datetime.now(UTC).isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {
            "ok": True,
            "restart_required": True,
            "staged_path": str(pending),
        }

    def apply_pending_restore(self) -> dict[str, Any] | None:
        marker = self.paths.runtime / "restore-pending.json"
        pending = self.paths.runtime / "restore-pending"
        if not marker.exists() or not pending.exists():
            return None
        database = pending / "jarvis.db"
        if database.exists():
            self.paths.database.parent.mkdir(parents=True, exist_ok=True)
            _remove_sqlite_sidecars(self.paths.database)
            os.replace(database, self.paths.database)
        config = pending / "config.json"
        if config.exists():
            os.replace(config, self.paths.config)
        persona = pending / "persona.json"
        if persona.exists():
            os.replace(persona, self.paths.root / "persona.json")
        restored = {
            "ok": True,
            "database": self.paths.database.exists(),
            "config": self.paths.config.exists(),
            "persona": (self.paths.root / "persona.json").exists(),
        }
        shutil.rmtree(pending, ignore_errors=True)
        marker.unlink(missing_ok=True)
        return restored


def _register_file(
    manifest: dict[str, Any],
    path: Path,
    root: Path,
) -> None:
    relative = path.relative_to(root).as_posix()
    data = path.read_bytes()
    manifest["files"][relative] = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        sidecar.unlink(missing_ok=True)


def _safe_label(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in value.strip()
    ).strip("-")[:48]
