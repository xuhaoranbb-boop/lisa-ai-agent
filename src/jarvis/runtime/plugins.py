from __future__ import annotations

import hashlib
import hmac
import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jarvis.runtime.contracts import PermissionLevel, ToolSpec
from jarvis.runtime.tooling import RuntimeTool, ToolRegistry
from jarvis.version import SemanticVersion


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    author: str = ""
    description: str = ""
    enabled: bool = False
    permissions: list[str] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
    signature: str = ""
    sha256: str = ""
    source: str = ""

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
        *,
        source: str = "",
    ) -> PluginManifest:
        name = str(value.get("name") or "").strip()
        version = str(value.get("version") or "").strip()
        if not name or not version:
            raise ValueError("Plugin manifest requires name and version.")
        return cls(
            name=name,
            version=version,
            author=str(value.get("author") or ""),
            description=str(value.get("description") or ""),
            enabled=bool(value.get("enabled", False)),
            permissions=[str(item) for item in value.get("permissions") or []],
            tools=[
                dict(item)
                for item in value.get("tools") or []
                if isinstance(item, dict)
            ],
            events=[str(item) for item in value.get("events") or []],
            settings=dict(value.get("settings") or {}),
            dependencies=[
                str(item) for item in value.get("dependencies") or []
            ],
            signature=str(value.get("signature") or ""),
            sha256=str(value.get("sha256") or "").lower(),
            source=source,
        )


class PluginManager:
    """Load plugin manifests and register approved plugin tools."""

    def __init__(
        self,
        *,
        signing_key: str = "",
        allowed_roots: tuple[Path, ...] = (),
    ) -> None:
        self._manifests: dict[str, PluginManifest] = {}
        self.signing_key = signing_key
        self.allowed_roots = tuple(path.expanduser().resolve() for path in allowed_roots)

    def load_file(self, path: Path) -> PluginManifest:
        resolved = path.expanduser().resolve()
        raw = json.loads(resolved.read_text(encoding="utf-8"))
        report = self.validate_manifest(
            raw,
            source=resolved,
            production=bool(self.signing_key),
        )
        if not report["installable"]:
            raise ValueError(
                "Plugin installation rejected: " + "; ".join(report["reasons"])
            )
        manifest = PluginManifest.from_dict(
            raw,
            source=str(resolved),
        )
        self._manifests[manifest.name] = manifest
        return manifest

    def load_directory(self, path: Path) -> list[PluginManifest]:
        resolved = path.expanduser().resolve()
        if not resolved.exists():
            return []
        manifests: list[PluginManifest] = []
        for manifest_path in sorted(
            [*resolved.glob("*.json"), *resolved.glob("*/plugin.json")]
        ):
            try:
                manifests.append(self.load_file(manifest_path))
            except Exception:
                continue
        return manifests

    def validate_manifest(
        self,
        value: dict[str, Any],
        *,
        source: Path | str,
        production: bool = False,
    ) -> dict[str, Any]:
        reasons: list[str] = []
        try:
            manifest = PluginManifest.from_dict(value, source=str(source))
            SemanticVersion.parse(_normalize_semver(manifest.version))
        except ValueError as exc:
            return {
                "installable": False,
                "signed": False,
                "reasons": [str(exc)],
            }
        if production and not manifest.author.strip():
            reasons.append("Plugin author is required.")
        unknown = [
            permission
            for permission in manifest.permissions
            if permission.strip().upper()
            not in {
                "READ_ONLY",
                "LOW_RISK",
                "NORMAL",
                "MEDIUM_RISK",
                "HIGH_RISK",
                "CRITICAL",
            }
        ]
        if unknown:
            reasons.append("Unknown permissions: " + ", ".join(unknown))
        if any(
            permission.strip().upper() in {"*", "ALL", "ADMIN", "SYSTEM.ALL"}
            for permission in manifest.permissions
        ):
            reasons.append("Plugins cannot request unrestricted system permissions.")
        if not _source_allowed(source, self.allowed_roots):
            reasons.append("Plugin source is not local or HTTPS.")

        payload = dict(value)
        payload.pop("signature", None)
        payload.pop("sha256", None)
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        signed = True
        if self.signing_key:
            expected = hmac.new(
                self.signing_key.encode("utf-8"),
                digest.encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            signed = bool(manifest.signature) and hmac.compare_digest(
                expected,
                manifest.signature.lower(),
            )
            if not signed:
                reasons.append("Plugin signature is missing or invalid.")
        if manifest.sha256 and manifest.sha256 != digest:
            reasons.append("Plugin manifest sha256 does not match its declared payload.")
        return {
            "installable": not reasons,
            "signed": signed,
            "digest": digest,
            "reasons": reasons,
            "manifest": manifest,
        }

    def install_manifest(
        self,
        path: Path,
        destination: Path,
        *,
        allow_unsigned: bool = False,
    ) -> PluginManifest:
        report = self.validate_manifest(
            json.loads(path.expanduser().resolve().read_text(encoding="utf-8")),
            source=path.expanduser().resolve(),
            production=True,
        )
        if not report["installable"]:
            raise ValueError(
                "Plugin installation rejected: " + "; ".join(report["reasons"])
            )
        if self.signing_key and not report["signed"] and not allow_unsigned:
            raise PermissionError("Unsigned plugins require explicit approval.")
        manifest: PluginManifest = report["manifest"]
        destination = destination.expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / "plugin.json"
        target.write_text(
            json.dumps(
                {
                    "name": manifest.name,
                    "version": manifest.version,
                    "author": manifest.author,
                    "description": manifest.description,
                    "enabled": False,
                    "permissions": manifest.permissions,
                    "tools": manifest.tools,
                    "events": manifest.events,
                    "settings": manifest.settings,
                    "dependencies": manifest.dependencies,
                    "signature": manifest.signature,
                    "sha256": manifest.sha256,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return self.load_file(target)

    def enable(self, name: str) -> bool:
        manifest = self._manifests.get(name)
        if manifest is None:
            return False
        self._manifests[name] = PluginManifest(
            **{
                **manifest.__dict__,
                "enabled": True,
            }
        )
        return True

    def manifests(self) -> list[PluginManifest]:
        return list(self._manifests.values())

    def register_tools(
        self,
        registry: ToolRegistry,
        *,
        allow_high_risk: bool = False,
    ) -> list[str]:
        registered: list[str] = []
        for manifest in self._manifests.values():
            if not manifest.enabled:
                continue
            for tool in manifest.tools:
                name = str(tool.get("name") or "").strip()
                handler_ref = str(tool.get("handler") or "").strip()
                if not name or not handler_ref:
                    continue
                permission = PermissionLevel(
                    str(tool.get("permission") or PermissionLevel.MEDIUM_RISK)
                )
                if permission in {
                    PermissionLevel.HIGH_RISK,
                    PermissionLevel.CRITICAL,
                } and not allow_high_risk:
                    continue
                handler = _resolve_handler(handler_ref)
                registry.register(
                    RuntimeTool(
                        spec=ToolSpec(
                            name=name,
                            description=str(tool.get("description") or ""),
                            parameters=dict(
                                tool.get("parameters")
                                or {
                                    "type": "object",
                                    "properties": {},
                                    "additionalProperties": False,
                                }
                            ),
                            permission=permission,
                            capability=str(tool.get("category") or "plugin").lower(),
                            category=str(tool.get("category") or "PLUGIN"),
                            risk_level=permission,
                            metadata={
                                "plugin": manifest.name,
                                "plugin_version": manifest.version,
                            },
                        ),
                        handler=handler,
                    )
                )
                registered.append(name)
        return registered


def _resolve_handler(reference: str):  # type: ignore[no-untyped-def]
    module_name, separator, function_name = reference.partition(":")
    if not separator or not module_name or not function_name:
        raise ValueError(
            f"Plugin handler must use module:function syntax: {reference}"
        )
    module = importlib.import_module(module_name)
    handler = getattr(module, function_name)
    if not callable(handler):
        raise TypeError(f"Plugin handler is not callable: {reference}")
    return handler


def _source_allowed(source: Path | str, allowed_roots: tuple[Path, ...]) -> bool:
    if isinstance(source, Path):
        resolved = source.expanduser().resolve()
        if allowed_roots:
            return any(
                root == resolved or root in resolved.parents
                for root in allowed_roots
            )
        return True
    parsed = urlparse(str(source))
    return parsed.scheme == "https"


def _normalize_semver(value: str) -> str:
    parts = value.strip().split(".")
    if len(parts) == 1:
        return f"{parts[0]}.0.0"
    if len(parts) == 2:
        return f"{value.strip()}.0"
    return value.strip()
