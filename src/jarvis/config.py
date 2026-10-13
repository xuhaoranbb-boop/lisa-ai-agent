from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from JARVIS_* environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="JARVIS_",
        extra="ignore",
    )

    assistant_name: str = "Jarvis"
    user_name: str = ""
    user_city: str = ""
    timezone: str = "Asia/Shanghai"
    persona_mode: Literal[
        "butler",
        "focused",
        "brief",
        "companion",
        "alert",
    ] = "butler"

    model: str = "qwen3:8b"
    voice_model: str = "qwen3:8b"
    reasoning_model: str = ""
    code_model: str = ""
    vision_model: str = ""
    vision_base_url: str = ""
    vision_api_key: SecretStr = SecretStr("")
    embedding_model: str = ""
    computer_model: str = ""
    model_fallbacks: str = ""
    model_fallback_roles: str = "chat,voice,reasoning,code,computer"
    model_fallback_base_url: str = ""
    model_fallback_api_key: SecretStr = SecretStr("")
    model_fallback_extra_body: str = ""
    model_budget_fallback: str = ""
    budget_downshift_ratio: float = Field(default=0.8, ge=0.1, le=1.0)
    model_base_url: str = "http://127.0.0.1:11434/v1"
    model_api_key: SecretStr = SecretStr("ollama")
    temperature: float = Field(default=0.25, ge=0.0, le=2.0)
    reasoning_effort: Literal["none", "low", "medium", "high", "max"] | None = "none"
    max_tokens: int = Field(default=2048, ge=128, le=32768)
    voice_max_tokens: int = Field(default=512, ge=64, le=4096)
    voice_max_iterations: int = Field(default=16, ge=1, le=24)
    voice_tts_chunk_chars: int = Field(default=100, ge=40, le=360)
    voice_tts_max_concurrent: int = Field(default=4, ge=1, le=6)
    voice_max_spoken_chars: int = Field(default=1_000, ge=60, le=4_000)
    max_iterations: int = Field(default=24, ge=1, le=48)
    request_timeout: float = Field(default=120.0, gt=0)
    keep_alive: str = "2h"

    data_dir: Path = Path("~/.jarvis")
    workspace: Path = Path.cwd()
    environment: Literal["development", "test", "production"] = "development"
    logging_level: Literal["DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"] = "INFO"
    logging_max_bytes: int = Field(default=10 * 1024 * 1024, ge=64_000)
    logging_retention_days: int = Field(default=14, ge=1, le=365)
    performance_mode: Literal["BALANCED", "LOW_POWER", "HIGH_PERFORMANCE"] = "BALANCED"
    vision_frequency_seconds: int = Field(default=8, ge=1, le=300)
    background_tasks_enabled: bool = True
    offline_mode: bool = False
    network_proxy: str = ""
    update_channel: Literal["stable", "beta"] = "stable"
    update_manifest_url: str = ""
    update_auto_check: bool = True
    close_to_tray: bool = True
    wake_word_enabled: bool = True

    max_history_messages: int = Field(default=24, ge=2, le=200)
    max_memory_results: int = Field(default=6, ge=1, le=20)
    context_max_chars: int = Field(default=18_000, ge=2_000, le=100_000)
    memory_extraction_enabled: bool = True
    proactive_enabled: bool = True
    proactive_min_priority: int = Field(default=3, ge=1, le=5)
    proactive_stale_task_minutes: int = Field(default=30, ge=1, le=1_440)
    proactive_quiet_hours: str = ""
    max_file_bytes: int = Field(default=256_000, ge=1_000, le=5_000_000)
    bridge_token: SecretStr = SecretStr("")
    amap_api_key: SecretStr = SecretStr("")
    update_signing_key: SecretStr = SecretStr("")
    plugin_signing_key: SecretStr = SecretStr("")
    browser_backend: Literal["web", "playwright"] = "web"
    browser_headless: bool = True
    request_token_budget: int = Field(default=12_000, ge=0, le=1_000_000)
    task_token_budget: int = Field(default=120_000, ge=0, le=10_000_000)
    daily_token_budget: int = Field(default=500_000, ge=0, le=100_000_000)
    input_cost_per_million: float = Field(default=0.0, ge=0.0)
    output_cost_per_million: float = Field(default=0.0, ge=0.0)
    plugin_dirs: str = ""
    permission_auto_approve: str = "READ_ONLY,LOW_RISK,NORMAL,MEDIUM_RISK"
    permission_allowed_apps: str = ""
    permission_allowed_folders: str = ""
    permission_allowed_commands: str = ""
    permission_allowed_message_recipients: str = "文件传输助手"
    permission_blocked_apps: str = ""
    permission_blocked_paths: str = ""
    permission_blocked_commands: str = ""
    mcp_servers: str = ""

    whisper_model: str = "small"
    whisper_partial_model: str = "base"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_initial_prompt: str = (
        "以下是中文语音对话。常用词：贾维斯、Jarvis、DeepSeek、Codex、MCP、"
        "Electron、Python、PowerShell、Ollama、VS Code、浏览器、微信、QQ、"
        "截图、打开、关闭、停止、暂停、继续、取消、运行测试、查看文件。"
    )
    asr_provider_order: str = "qwen3-asr,paraformer-zh-streaming,sensevoice,whisper"
    asr_sherpa_model_dir: str = (
        "~/.jarvis/models/sherpa-onnx/streaming-paraformer-bilingual-zh-en"
    )
    asr_sherpa_num_threads: int = Field(default=2, ge=1, le=8)
    asr_sherpa_provider: str = "cpu"
    asr_sensevoice_model_dir: str = (
        "~/.jarvis/models/sherpa-onnx/sense-voice-zh-en-ja-ko-yue-2024-07-17"
    )
    asr_sensevoice_num_threads: int = Field(default=2, ge=1, le=8)
    asr_sensevoice_provider: str = "cpu"
    asr_sensevoice_language: str = "auto"
    asr_hotwords: str = (
        "贾维斯,Jarvis,DeepSeek,Codex,MCP,Electron,Python,PowerShell,VS Code,"
        "Ollama,浏览器,微信,QQ,截图,打开,关闭,停止,暂停,继续,取消"
    )
    asr_partial_window_seconds: float = Field(default=8.0, ge=1.0, le=30.0)
    asr_partial_interval_seconds: float = Field(default=0.4, ge=0.2, le=2.0)
    asr_no_speech_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    asr_final_beam_size: int = Field(default=2, ge=1, le=5)
    asr_final_vad_filter: bool = True
    asr_short_final_seconds: float = Field(default=2.5, ge=0.5, le=10.0)
    asr_short_final_beam_size: int = Field(default=2, ge=1, le=5)
    tts_backend: Literal["edge", "piper"] = "edge"
    tts_voice: str = "zh-CN-YunyangNeural"
    tts_piper_voice: str = "zh_CN-huayan-medium"
    tts_rate: str = "+25%"
    tts_length_scale: float = Field(default=0.82, ge=0.4, le=2.0)

    @field_validator("data_dir", "workspace", mode="before")
    @classmethod
    def expand_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser()

    @field_validator("data_dir", "workspace")
    @classmethod
    def resolve_path(cls, value: Path) -> Path:
        return value.resolve()

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def empty_reasoning_effort_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jarvis.db"

    def ensure_data_dir(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir
