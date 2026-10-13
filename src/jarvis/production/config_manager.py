from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from threading import RLock
from typing import Any

from jarvis.config import Settings
from jarvis.production.secrets import SecretManager

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "environment": "development",
    "llm": {
        "model": "qwen3:8b",
        "voice_model": "qwen3:8b",
        "reasoning_model": "",
        "code_model": "",
        "vision_model": "",
        "vision_base_url": "",
        "embedding_model": "",
        "base_url": "http://127.0.0.1:11434/v1",
        "temperature": 0.25,
        "max_tokens": 2048,
        "timeout": 120.0,
    },
    "asr": {
        "model": "small",
        "partial_model": "base",
        "device": "cpu",
        "compute_type": "int8",
        "initial_prompt": (
            "以下是中文语音对话。常用词：贾维斯、Jarvis、DeepSeek、Ollama、"
            "VS Code、打开应用、运行测试、查看文件、记住、继续、停止。"
        ),
        "hotwords": "贾维斯,Jarvis,DeepSeek,VS Code,Ollama",
        "partial_window_seconds": 8.0,
        "partial_interval_seconds": 0.4,
        "no_speech_threshold": 0.6,
        "final_beam_size": 2,
        "final_vad_filter": True,
    },
    "tts": {
        "backend": "edge",
        "voice": "zh-CN-YunyangNeural",
        "piper_voice": "zh_CN-huayan-medium",
        "rate": "+25%",
        "length_scale": 0.82,
    },
    "database": {"path": "~/.jarvis/jarvis.db"},
    "logging": {
        "level": "INFO",
        "max_bytes": 10 * 1024 * 1024,
        "retention_days": 14,
    },
    "performance": {
        "mode": "BALANCED",
        "vision_frequency_seconds": 8,
        "context_max_chars": 18_000,
        "background_tasks_enabled": True,
    },
    "voice": {
        "tts_chunk_chars": 100,
        "tts_max_concurrent": 4,
        "max_spoken_chars": 1_000,
    },
    "network": {
        "offline_mode": False,
        "proxy": "",
    },
    "mcp": {"servers": []},
    "updates": {
        "channel": "stable",
        "manifest_url": "",
        "auto_check": True,
    },
    "ui": {
        "close_to_tray": True,
        "wake_word_enabled": True,
        "muted": False,
        "hotkeys": {
            "wake": "CommandOrControl+Shift+J",
            "stop": "CommandOrControl+Shift+Space",
            "pause": "",
            "resume": "",
        },
    },
    "permissions": {
        "desktop": "ASK_EVERY_TIME",
        "files": "ASK_EVERY_TIME",
        "browser": "ASK_EVERY_TIME",
        "network": "ASK_EVERY_TIME",
        "mcp": "ASK_EVERY_TIME",
        "system": "ASK_EVERY_TIME",
        "allowed_apps": "*",
    },
}

SETTINGS_PATH_MAP: dict[str, str] = {
    "llm.model": "model",
    "llm.voice_model": "voice_model",
    "llm.reasoning_model": "reasoning_model",
    "llm.code_model": "code_model",
    "llm.vision_model": "vision_model",
    "llm.vision_base_url": "vision_base_url",
    "llm.embedding_model": "embedding_model",
    "llm.base_url": "model_base_url",
    "llm.temperature": "temperature",
    "llm.max_tokens": "max_tokens",
    "llm.timeout": "request_timeout",
    "asr.model": "whisper_model",
    "asr.partial_model": "whisper_partial_model",
    "asr.device": "whisper_device",
    "asr.compute_type": "whisper_compute_type",
    "asr.initial_prompt": "whisper_initial_prompt",
    "asr.hotwords": "asr_hotwords",
    "asr.partial_window_seconds": "asr_partial_window_seconds",
    "asr.partial_interval_seconds": "asr_partial_interval_seconds",
    "asr.no_speech_threshold": "asr_no_speech_threshold",
    "asr.final_beam_size": "asr_final_beam_size",
    "asr.final_vad_filter": "asr_final_vad_filter",
    "tts.voice": "tts_voice",
    "tts.backend": "tts_backend",
    "tts.piper_voice": "tts_piper_voice",
    "tts.rate": "tts_rate",
    "tts.length_scale": "tts_length_scale",
    "database.path": "data_dir",
    "logging.level": "logging_level",
    "logging.max_bytes": "logging_max_bytes",
    "logging.retention_days": "logging_retention_days",
    "performance.mode": "performance_mode",
    "performance.vision_frequency_seconds": "vision_frequency_seconds",
    "performance.context_max_chars": "context_max_chars",
    "performance.background_tasks_enabled": "background_tasks_enabled",
    "voice.tts_chunk_chars": "voice_tts_chunk_chars",
    "voice.tts_max_concurrent": "voice_tts_max_concurrent",
    "voice.max_spoken_chars": "voice_max_spoken_chars",
    "network.offline_mode": "offline_mode",
    "network.proxy": "network_proxy",
    "updates.channel": "update_channel",
    "updates.manifest_url": "update_manifest_url",
    "updates.auto_check": "update_auto_check",
    "ui.close_to_tray": "close_to_tray",
    "ui.wake_word_enabled": "wake_word_enabled",
    "permissions.allowed_apps": "permission_allowed_apps",
}

SECRET_PATH_MAP: dict[str, str] = {
    "llm.api_key": "model_api_key",
    "llm.vision_api_key": "vision_api_key",
    "bridge.token": "bridge_token",
    "updates.signing_key": "update_signing_key",
    "plugins.signing_key": "plugin_signing_key",
}


class ConfigManager:
    """Merge defaults, user settings, environment, and secure secrets."""

    def __init__(
        self,
        path: Path,
        *,
        secret_manager: SecretManager | None = None,
        environment: dict[str, str] | None = None,
        defaults: dict[str, Any] | None = None,
    ) -> None:
        self.path = path
        self.secret_manager = secret_manager
        self.environment = environment if environment is not None else dict(os.environ)
        self.defaults = copy.deepcopy(defaults or DEFAULT_CONFIG)
        self._lock = RLock()
        self._user_config: dict[str, Any] = {}
        self.last_error = ""
        self.load()
        self._absorb_user_secrets()

    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                self._user_config = {}
                return self._user_config
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                self._user_config = payload if isinstance(payload, dict) else {}
                self.last_error = ""
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                quarantine = self.path.with_suffix(".invalid.json")
                try:
                    self.path.replace(quarantine)
                except OSError:
                    pass
                self._user_config = {}
            return copy.deepcopy(self._user_config)

    def effective(self, section: str | None = None) -> dict[str, Any]:
        with self._lock:
            merged = _deep_merge(self.defaults, self._user_config)
            for path, field_name in SETTINGS_PATH_MAP.items():
                environment_key = f"JARVIS_{field_name.upper()}"
                if environment_key in self.environment:
                    _set_path(merged, path, self.environment[environment_key])
            for path, secret_name in SECRET_PATH_MAP.items():
                configured = (
                    self.secret_manager.has(secret_name)
                    if self.secret_manager is not None
                    else False
                )
                if configured:
                    _set_path(merged, path, self.secret_manager.mask(secret_name))
                elif _get_path(merged, path, ""):
                    _set_path(merged, path, "[REDACTED]")
            if section:
                value = _get_path(merged, section)
                return copy.deepcopy(value) if isinstance(value, dict) else {"value": value}
            return merged

    def get(self, path: str, default: Any = None) -> Any:
        return copy.deepcopy(_get_path(self.effective(), path, default))

    def set(self, path: str, value: Any, *, persist: bool = True) -> None:
        if path in SECRET_PATH_MAP:
            if self.secret_manager is None:
                raise ValueError("SecretManager is required for secret configuration.")
            self.secret_manager.set(
                SECRET_PATH_MAP[path],
                str(value),
                persist=persist,
            )
            return
        with self._lock:
            _set_path(self._user_config, path, value)
            self._user_config.setdefault("schema_version", 1)
            if persist:
                self.save()

    def update(self, values: dict[str, Any], *, persist: bool = True) -> None:
        for path in _flatten_paths(values):
            self.set(path, _get_path(values, path), persist=False)
        if persist:
            self.save()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(self._user_config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)

    def settings_overrides(self) -> dict[str, Any]:
        overrides: dict[str, Any] = {}
        merged = _deep_merge(self.defaults, self._user_config)
        for path, field_name in SETTINGS_PATH_MAP.items():
            if f"JARVIS_{field_name.upper()}" in self.environment:
                continue
            value = _get_path(merged, path)
            if value is not None:
                overrides[field_name] = value
        if self.secret_manager is not None:
            for path, secret_name in SECRET_PATH_MAP.items():
                if path == "database.path":
                    continue
                field_name = SECRET_PATH_MAP[path]
                environment_key = f"JARVIS_{field_name.upper()}"
                if environment_key in self.environment:
                    continue
                if self.secret_manager.has(secret_name):
                    overrides[field_name] = self.secret_manager.get(secret_name)
        return overrides

    def apply_to_settings(self, settings: Settings) -> Settings:
        updates = {
            key: value
            for key, value in self.settings_overrides().items()
            if key in Settings.model_fields
        }
        return settings.model_copy(update=updates)

    def public_settings(self) -> dict[str, Any]:
        payload = self.effective()
        for path in SECRET_PATH_MAP:
            if _get_path(payload, path, None) is not None:
                _set_path(payload, path, "[REDACTED]")
        return payload

    def __repr__(self) -> str:
        return f"ConfigManager(path={str(self.path)!r}, secrets=[REDACTED])"

    def _absorb_user_secrets(self) -> None:
        changed = False
        for path, secret_name in SECRET_PATH_MAP.items():
            value = _get_path(self._user_config, path, "")
            if not value or value == "[REDACTED]":
                continue
            if self.secret_manager is not None:
                self.secret_manager.set(secret_name, str(value))
            _set_path(self._user_config, path, "[REDACTED]")
            changed = True
        if changed:
            self.save()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _get_path(value: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _set_path(value: dict[str, Any], path: str, item: Any) -> None:
    parts = path.split(".")
    current = value
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = copy.deepcopy(item)


def _flatten_paths(value: dict[str, Any], prefix: str = "") -> list[str]:
    paths: list[str] = []
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            paths.extend(_flatten_paths(item, path))
        else:
            paths.append(path)
    return paths
