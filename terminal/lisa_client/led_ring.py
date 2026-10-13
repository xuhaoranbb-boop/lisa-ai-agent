"""WS2812B LED 灯环控制

支持两种模式：
  1. SPI 模式（推荐）— 通过 /dev/spidev0.0 直接驱动，时序精准
  2. 模拟模式 — 仅打印颜色到日志（开发调试用）

灯效状态：
  IDLE      — 蓝色呼吸
  RECORDING — 红色常亮
  THINKING  — 橙色旋转
  WORKING   — 紫色常亮
  SPEAKING  — 绿色呼吸
  ERROR     — 红色闪烁
  OFFLINE   — 黄色微亮
"""

from __future__ import annotations

import logging
import math
import struct
import time
from enum import StrEnum

logger = logging.getLogger(__name__)

# 尝试导入 spidev
try:
    import spidev
    _SPI_AVAILABLE = True
except ImportError:
    _SPI_AVAILABLE = False


class LedState(StrEnum):
    IDLE = "IDLE"
    RECORDING = "RECORDING"
    THINKING = "THINKING"
    WORKING = "WORKING"
    SPEAKING = "SPEAKING"
    ERROR = "ERROR"
    OFFLINE = "OFFLINE"


# WS2812B 协议：每个 LED 用 24 bit 表示 (GRB)
# SPI 采样率需要足够高来模拟 WS2812 时序
# 使用 2.4MHz SPI，每个 WS2812 bit 用 3 个 SPI byte 表示
SPI_SPEED_HZ = 2_400_000
BITS_PER_WS_BYTE = 3  # 每个 WS2812 bit 用 3 个 SPI bit


class LEDRing:
    """WS2812B 灯环驱动"""

    def __init__(
        self,
        count: int = 12,
        spi_device: str = "/dev/spidev0.0",
        brightness: int = 80,
        use_spi: bool = True,
    ):
        self.count = count
        self.brightness = brightness
        self.use_spi = use_spi and _SPI_AVAILABLE
        self.spi_device = spi_device

        self._spi: spidev.SpiDev | None = None
        self._pixels = [(0, 0, 0)] * count  # (R, G, B) per pixel
        self._state = LedState.IDLE
        self._animation_task = None

    def start(self) -> None:
        """初始化 SPI"""
        if self.use_spi:
            try:
                self._spi = spidev.SpiDev()
                bus, dev = self._parse_spi_device()
                self._spi.open(bus, dev)
                self._spi.max_speed_hz = SPI_SPEED_HZ
                self._spi.mode = 0
                logger.info("LED ring SPI initialized: %s, %d LEDs", self.spi_device, self.count)
            except Exception as exc:
                logger.warning("SPI init failed: %s — falling back to log mode", exc)
                self.use_spi = False
        else:
            logger.info("LED ring in log-only mode (no SPI)")

    def _parse_spi_device(self) -> tuple[int, int]:
        """解析 /dev/spidevX.Y → (X, Y)"""
        parts = self.spi_device.replace("/dev/spidev", "").split(".")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0

    def set_color(self, r: int, g: int, b: int) -> None:
        """设置所有灯珠为同一颜色"""
        r, g, b = self._clamp(r, g, b)
        self._pixels = [(r, g, b)] * self.count
        self._flush()

    def set_from_bridge(self, r: int, g: int, b: int, mode: str = "solid") -> None:
        """响应 bridge 下发的 LED 命令"""
        r, g, b = self._clamp(r, g, b)
        if mode == "solid":
            self._pixels = [(r, g, b)] * self.count
            self._flush()
        elif mode == "breathing":
            # 呼吸效果只设目标色，由动画循环处理
            self._pixels = [(r, g, b)] * self.count
            self._flush()
        elif mode == "spin":
            # 旋转效果
            self._pixels = [(r, g, b)] * self.count
            self._flush()

    def set_state(self, state: LedState) -> None:
        """根据状态设置预设灯效"""
        self._state = state
        presets = {
            LedState.IDLE: (0, 0, 80),
            LedState.RECORDING: (150, 0, 0),
            LedState.THINKING: (120, 80, 0),
            LedState.WORKING: (80, 0, 160),
            LedState.SPEAKING: (0, 150, 0),
            LedState.ERROR: (160, 0, 0),
            LedState.OFFLINE: (60, 40, 0),
        }
        r, g, b = presets.get(state, (0, 0, 0))
        self.set_color(r, g, b)

    def off(self) -> None:
        """关闭所有灯"""
        self._pixels = [(0, 0, 0)] * self.count
        self._flush()

    def _clamp(self, r: int, g: int, b: int) -> tuple[int, int, int]:
        """限幅 + 亮度缩放"""
        scale = self.brightness / 255
        return (
            max(0, min(255, int(r * scale))),
            max(0, min(255, int(g * scale))),
            max(0, min(255, int(b * scale))),
        )

    def _flush(self) -> None:
        """把像素数据写到 SPI 或日志"""
        if self.use_spi and self._spi:
            try:
                spi_data = self._encode_ws2812_spi()
                # WS2812B 需要 >= 50μs 的 reset 信号（全零）
                reset_bytes = b"\x00" * 100
                self._spi.xfer2(list(spi_data))
                time.sleep(0.001)  # 1ms reset
            except Exception as exc:
                logger.warning("SPI write failed: %s", exc)
        else:
            # 日志模式
            if self._pixels:
                r, g, b = self._pixels[0]
                logger.debug("LED: (%d, %d, %d) x%d", r, g, b, self.count)

    def _encode_ws2812_spi(self) -> bytes:
        """把 GRB 数据编码成 SPI 字节流

        WS2812B 时序（@ 2.4MHz SPI，每 bit = 0.417μs）：
          - '0' bit: ~0.4μs high + ~0.85μs low → SPI: 0b11000000 (0xC0)
          - '1' bit: ~0.8μs high + ~0.45μs low → SPI: 0b11111000 (0xF8)

        简化方案：每个 WS2812 bit 用 1 个 SPI byte
          '0' → 0b11000000
          '1' → 0b11111000
        """
        result = bytearray()
        for r, g, b in self._pixels:
            # WS2812B 顺序: G, R, B
            for value in (g, r, b):
                for bit_pos in range(7, -1, -1):
                    bit = (value >> bit_pos) & 1
                    if bit:
                        result.append(0xF8)
                    else:
                        result.append(0xC0)
        return bytes(result)

    def cleanup(self) -> None:
        """释放 SPI 资源"""
        self.off()
        if self._spi:
            try:
                self._spi.close()
            except Exception:
                pass
        logger.info("LED ring cleanup done")
