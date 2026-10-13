from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from jarvis.production.backup import BackupManager
from jarvis.production.environment import RuntimePaths
from jarvis.production.secrets import SecretManager
from jarvis.version import SemanticVersion, current_version


class UpdateStatus(StrEnum):
    IDLE = "IDLE"
    AVAILABLE = "AVAILABLE"
    UP_TO_DATE = "UP_TO_DATE"
    DOWNLOADING = "DOWNLOADING"
    READY = "READY"
    INSTALLING = "INSTALLING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class UpdateManifest:
    version: str
    url: str
    sha256: str
    signature: str = ""
    channel: str = "stable"
    notes: str = ""
    minimum_version: str = "0.0.0"
    installer_args: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> UpdateManifest:
        version = str(payload.get("version") or "").strip()
        url = str(payload.get("url") or "").strip()
        sha256 = str(payload.get("sha256") or "").strip().lower()
        if not version or not url or not sha256:
            raise ValueError("Update manifest requires version, url, and sha256.")
        SemanticVersion.parse(version)
        if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
            raise ValueError("Update manifest sha256 is invalid.")
        args = payload.get("installer_args") or []
        if not isinstance(args, list):
            raise ValueError("installer_args must be a list.")
        return cls(
            version=version,
            url=url,
            sha256=sha256,
            signature=str(payload.get("signature") or "").strip(),
            channel=str(payload.get("channel") or "stable"),
            notes=str(payload.get("notes") or ""),
            minimum_version=str(payload.get("minimum_version") or "0.0.0"),
            installer_args=tuple(str(item) for item in args),
        )

    def signing_payload(self) -> bytes:
        return "\n".join(
            [
                self.version,
                self.channel,
                self.url,
                self.sha256,
            ]
        ).encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "url": self.url,
            "sha256": self.sha256,
            "signature": "[REDACTED]" if self.signature else "",
            "channel": self.channel,
            "notes": self.notes,
            "minimum_version": self.minimum_version,
            "installer_args": list(self.installer_args),
        }


class UpdateManager:
    """Verify, download, install, and roll back explicitly approved updates."""

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        secrets: SecretManager,
        backups: BackupManager,
    ) -> None:
        self.paths = paths
        self.secrets = secrets
        self.backups = backups
        self.status = UpdateStatus.IDLE
        self.last_error = ""
        self.available_manifest: UpdateManifest | None = None
        self.downloaded_path: Path | None = None

    async def check(
        self,
        manifest_url: str,
        *,
        channel: str = "stable",
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        parsed = urlparse(manifest_url)
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("Update manifest must use HTTPS.")
        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        try:
            response = await client.get(manifest_url)
            response.raise_for_status()
            manifest = UpdateManifest.from_dict(response.json())
        finally:
            if owns_client:
                await client.aclose()
        if manifest.channel != channel:
            self.status = UpdateStatus.REJECTED
            self.last_error = "Update channel does not match the configured channel."
            return self.snapshot()
        installed = current_version()
        minimum = SemanticVersion.parse(manifest.minimum_version)
        if installed < minimum:
            self.status = UpdateStatus.REJECTED
            self.last_error = f"Installed version must be at least {minimum}."
            return self.snapshot()
        self.available_manifest = manifest
        if SemanticVersion.parse(manifest.version) <= installed:
            self.status = UpdateStatus.UP_TO_DATE
            self.last_error = ""
        else:
            self.status = UpdateStatus.AVAILABLE
            self.last_error = ""
        return self.snapshot()

    async def download(
        self,
        manifest: UpdateManifest | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        manifest = manifest or self.available_manifest
        if manifest is None:
            raise ValueError("No update manifest has been checked.")
        if not self.verify_signature(manifest):
            self.status = UpdateStatus.REJECTED
            self.last_error = "Update signature is missing or invalid."
            return self.snapshot()
        self.status = UpdateStatus.DOWNLOADING
        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=120.0, follow_redirects=True)
        self.paths.updates.mkdir(parents=True, exist_ok=True)
        extension = Path(urlparse(manifest.url).path).suffix or ".bin"
        destination = self.paths.updates / f"jarvis-{manifest.version}{extension}"
        digest = hashlib.sha256()
        try:
            async with client.stream("GET", manifest.url) as response:
                response.raise_for_status()
                with destination.open("wb") as file:
                    async for chunk in response.aiter_bytes():
                        digest.update(chunk)
                        file.write(chunk)
            if digest.hexdigest() != manifest.sha256:
                destination.unlink(missing_ok=True)
                self.status = UpdateStatus.REJECTED
                self.last_error = "Downloaded update checksum does not match."
                return self.snapshot()
            self.downloaded_path = destination
            self.status = UpdateStatus.READY
            self.last_error = ""
            return self.snapshot()
        except Exception as exc:
            destination.unlink(missing_ok=True)
            self.status = UpdateStatus.FAILED
            self.last_error = f"{type(exc).__name__}: {exc}"
            return self.snapshot()
        finally:
            if owns_client:
                await client.aclose()

    def install(
        self,
        *,
        confirmed: bool,
        manifest: UpdateManifest | None = None,
        installer_path: Path | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise PermissionError("Update installation requires explicit confirmation.")
        manifest = manifest or self.available_manifest
        installer = installer_path or self.downloaded_path
        if manifest is None or installer is None or not installer.exists():
            raise ValueError("A verified update must be downloaded before installation.")
        if not self.verify_signature(manifest):
            self.status = UpdateStatus.REJECTED
            self.last_error = "Update signature is invalid."
            return self.snapshot()
        backup = self.backups.create(kind="pre-update", label=manifest.version)
        self.status = UpdateStatus.INSTALLING
        try:
            completed = subprocess.run(
                [str(installer), *manifest.installer_args],
                check=False,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if completed.returncode != 0:
                self.status = UpdateStatus.FAILED
                self.last_error = completed.stderr.strip() or (
                    f"Installer exited with code {completed.returncode}."
                )
            else:
                self.status = UpdateStatus.VERIFIED
                self.last_error = ""
        except Exception as exc:
            self.status = UpdateStatus.FAILED
            self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self.paths.runtime.mkdir(parents=True, exist_ok=True)
            (self.paths.runtime / "last-update.json").write_text(
                json.dumps(
                    {
                        "manifest": manifest.to_dict(),
                        "backup": str(backup.path),
                        "status": str(self.status),
                        "error": self.last_error,
                        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return self.snapshot()

    def rollback(
        self,
        *,
        confirmed: bool,
        backup_path: Path | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise PermissionError("Update rollback requires explicit confirmation.")
        if backup_path is None:
            last_update = self.paths.runtime / "last-update.json"
            if not last_update.exists():
                raise ValueError("No previous update backup is available.")
            payload = json.loads(last_update.read_text(encoding="utf-8"))
            backup_path = Path(str(payload.get("backup") or ""))
        if not backup_path.exists():
            raise ValueError("Previous update backup does not exist.")
        result = self.backups.stage_restore(backup_path, confirmed=True)
        self.status = UpdateStatus.READY
        return {
            "ok": True,
            "rollback_staged": True,
            "restart_required": result["restart_required"],
        }

    def verify_signature(self, manifest: UpdateManifest) -> bool:
        key = self.secrets.get("update_signing_key")
        if not key or not manifest.signature:
            return False
        expected = hmac.new(
            key.encode("utf-8"),
            manifest.signing_payload(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, manifest.signature.lower())

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "current_version": str(current_version()),
            "available": (
                self.available_manifest.to_dict()
                if self.available_manifest is not None
                else None
            ),
            "signature_verified": (
                self.verify_signature(self.available_manifest)
                if self.available_manifest is not None
                else False
            ),
            "downloaded_path": (
                str(self.downloaded_path) if self.downloaded_path is not None else ""
            ),
            "last_error": self.last_error,
        }
