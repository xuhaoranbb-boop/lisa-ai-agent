"""配置加载"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class BridgeConfig:
    host: str = "192.168.1.100"
    port: int = 8765
    token: str = ""
    device_id: str = "lisa-opi5-01"
    reconnect_interval: int = 5
    heartbeat_interval: int = 15

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/ws/device?token={self.token}&device_id={self.device_id}"


@dataclass
class AudioConfig:
    sample_rate: int = 16000
    channels: int = 1
    input_device: str | None = None
    output_device: str | None = None
    block_size: int = 1024
    silence_threshold: int = 300
    max_record_seconds: int = 60


@dataclass
class GPIOConfig:
    button_pin: int = 27
    button_active_low: bool = True
    button_debounce_ms: int = 50


@dataclass
class LEDConfig:
    count: int = 12
    data_pin: int = 18
    brightness: int = 80
    use_spi: bool = True
    spi_device: str = "/dev/spidev0.0"


@dataclass
class LocalASRConfig:
    enabled: bool = True
    model_size: str = "small"
    language: str = "zh"
    vad_filter: bool = True
    beam_size: int = 5


@dataclass
class DisplayConfig:
    enabled: bool = False
    type: str = "ssd1306"
    bus: str = "i2c"
    i2c_address: str = "0x3C"
    i2c_bus: int = 7


@dataclass
class Config:
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    gpio: GPIOConfig = field(default_factory=GPIOConfig)
    led: LEDConfig = field(default_factory=LEDConfig)
    local_asr: LocalASRConfig = field(default_factory=LocalASRConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    log_level: str = "INFO"
    log_file: str | None = "/var/log/lisa-terminal.log"


def _load_section(cls: type, data: dict[str, Any] | None) -> Any:
    """从 dict 加载 dataclass，忽略未知字段"""
    if not data:
        return cls()
    valid_fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore
    filtered = {k: v for k, v in data.items() if k in valid_fields}
    return cls(**filtered)


def load_config(path: str | Path = "config.yaml") -> Config:
    """从 YAML 文件加载配置"""
    path = Path(path)
    if not path.exists():
        logger.warning("Config file not found: %s, using defaults", path)
        return Config()

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    cfg = Config(
        bridge=_load_section(BridgeConfig, raw.get("bridge")),
        audio=_load_section(AudioConfig, raw.get("audio")),
        gpio=_load_section(GPIOConfig, raw.get("gpio")),
        led=_load_section(LEDConfig, raw.get("led")),
        local_asr=_load_section(LocalASRConfig, raw.get("local_asr")),
        display=_load_section(DisplayConfig, raw.get("display")),
    )

    # 日志配置
    log_cfg = raw.get("logging", {})
    cfg.log_level = log_cfg.get("level", "INFO")
    cfg.log_file = log_cfg.get("file")

    # 校验
    if not cfg.bridge.token or cfg.bridge.token == "change-me-to-your-jarvis-token":
        logger.error("Bridge token not configured! Edit config.yaml first.")

    return cfg
