from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from threading import RLock
from typing import Any


class SecretPersistenceUnavailable(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


class _PlatformSecretProtector:
    def available(self) -> bool:
        return os.name == "nt"

    def protect(self, value: bytes, *, description: str) -> bytes:
        if not self.available():
            raise SecretPersistenceUnavailable(
                "Secure secret persistence is only available on Windows DPAPI."
            )
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        input_blob, input_buffer = _blob(value)
        output_blob = _DataBlob()
        success = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            description,
            None,
            None,
            None,
            0,
            ctypes.byref(output_blob),
        )
        del input_buffer
        if not success:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)

    def unprotect(self, value: bytes, *, description: str) -> bytes:
        if not self.available():
            raise SecretPersistenceUnavailable(
                "Secure secret persistence is only available on Windows DPAPI."
            )
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        input_blob, input_buffer = _blob(value)
        output_blob = _DataBlob()
        success = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(output_blob),
        )
        del input_buffer
        if not success:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)


class SecretManager:
    """Resolve secrets from environment first, then a DPAPI-backed vault."""

    def __init__(
        self,
        vault_path: Path,
        *,
        environment: dict[str, str] | None = None,
        protector: _PlatformSecretProtector | None = None,
    ) -> None:
        self.vault_path = vault_path
        self.environment = environment if environment is not None else dict(os.environ)
        self.protector = protector or _PlatformSecretProtector()
        self._lock = RLock()
        self._cache: dict[str, str] = {}
        self.last_error = ""

    def get(self, name: str, default: str = "") -> str:
        env_value = self._environment_value(name)
        if env_value is not None:
            return env_value
        with self._lock:
            if name not in self._cache:
                self._load()
            return self._cache.get(name, default)

    def has(self, name: str) -> bool:
        return bool(self.get(name))

    def set(self, name: str, value: str, *, persist: bool = True) -> None:
        normalized = _normalize_name(name)
        with self._lock:
            self._cache[normalized] = value
            if persist:
                self._save()

    def delete(self, name: str, *, persist: bool = True) -> bool:
        normalized = _normalize_name(name)
        with self._lock:
            if normalized not in self._cache and not self.vault_path.exists():
                return False
            self._load()
            existed = self._cache.pop(normalized, None) is not None
            if persist:
                self._save()
            return existed

    def names(self) -> list[str]:
        with self._lock:
            self._load()
            names = set(self._cache)
        names.update(
            key[7:].lower()
            for key in self.environment
            if key.startswith("JARVIS_SECRET_")
        )
        return sorted(names)

    def status(self) -> dict[str, Any]:
        names = self.names()
        return {
            "persistence_available": self.protector.available(),
            "vault_exists": self.vault_path.exists(),
            "secret_names": names,
            "secrets": {
                name: {"configured": bool(self.get(name)), "masked": self.mask(name)}
                for name in names
            },
            "last_error": self.last_error,
        }

    def mask(self, name: str) -> str:
        value = self.get(name)
        if not value:
            return ""
        if value.startswith("sk-") and len(value) > 8:
            return f"{value[:3]}********{value[-4:]}"
        if len(value) <= 8:
            return "********"
        return f"{value[:2]}********{value[-2:]}"

    def _environment_value(self, name: str) -> str | None:
        normalized = _normalize_name(name)
        candidates = (
            normalized.upper(),
            f"JARVIS_{normalized.upper()}",
            f"JARVIS_SECRET_{normalized.upper()}",
        )
        for candidate in candidates:
            if candidate in self.environment and self.environment[candidate] != "":
                return self.environment[candidate]
        return None

    def _load(self) -> None:
        if not self.vault_path.exists():
            return
        try:
            payload = json.loads(self.vault_path.read_text(encoding="utf-8"))
            ciphertext = base64.b64decode(str(payload["ciphertext"]))
            plaintext = self.protector.unprotect(
                ciphertext,
                description="GCAI JARVIS secret vault",
            )
            decoded = json.loads(plaintext.decode("utf-8"))
            if not isinstance(decoded, dict):
                raise ValueError("Secret vault payload must be an object.")
            self._cache = {
                _normalize_name(str(key)): str(value)
                for key, value in decoded.items()
            }
            self.last_error = ""
        except Exception as exc:
            self.last_error = type(exc).__name__
            self._cache = {}

    def _save(self) -> None:
        if not self.protector.available():
            raise SecretPersistenceUnavailable(
                "Secure secret persistence is unavailable on this platform."
            )
        plaintext = json.dumps(
            self._cache,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        ciphertext = self.protector.protect(
            plaintext,
            description="GCAI JARVIS secret vault",
        )
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.vault_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.vault_path)

    def __repr__(self) -> str:
        return f"SecretManager(vault_path={str(self.vault_path)!r}, secrets=[REDACTED])"


def _normalize_name(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def _blob(value: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(value)
    return (
        _DataBlob(
            len(value),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)),
        ),
        buffer,
    )
