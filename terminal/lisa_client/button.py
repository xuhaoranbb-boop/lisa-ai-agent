"""GPIO 按钮输入

支持 Orange Pi 5 Pro 的物理按钮。
按下 → 开始录音；松开 → 停止录音。
使用 OPi.GPIO 库，线程安全。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

logger = logging.getLogger(__name__)

# 尝试导入 GPIO 库，开发机上可能没有
try:
    import OPi.GPIO as GPIO
    _GPIO_AVAILABLE = True
except ImportError:
    _GPIO_AVAILABLE = False
    logger.info("OPi.GPIO not available — button will use keyboard fallback")


class Button:
    """物理录音按钮（PTT 按住说话）"""

    def __init__(
        self,
        pin: int = 27,
        active_low: bool = True,
        debounce_ms: int = 50,
    ):
        self.pin = pin
        self.active_low = active_low
        self.debounce_ms = debounce_ms
        self._pressed_cb: Callable[[], None] | None = None
        self._released_cb: Callable[[], None] | None = None
        self._running = False

    def on_press(self, callback: Callable[[], None]) -> None:
        self._pressed_cb = callback

    def on_release(self, callback: Callable[[], None]) -> None:
        self._released_cb = callback

    def start(self) -> None:
        """初始化 GPIO 并开始监听"""
        if not _GPIO_AVAILABLE:
            logger.warning("GPIO not available — button disabled (use keyboard 'f' key instead)")
            return

        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.pin, GPIO.IN, pull_up_down=GPIO.PUD_UP if self.active_low else GPIO.PUD_DOWN)

            edge = GPIO.FALLING if self.active_low else GPIO.RISING
            GPIO.add_event_detect(
                self.pin,
                edge,
                callback=self._on_edge,
                bouncetime=self.debounce_ms,
            )
            # 松开检测用双边沿
            if self.active_low:
                GPIO.add_event_detect(
                    self.pin,
                    GPIO.RISING,
                    callback=self._on_release_edge,
                    bouncetime=self.debounce_ms,
                )

            self._running = True
            logger.info("Button initialized: pin=%d, active_low=%s", self.pin, self.active_low)
        except Exception as exc:
            logger.error("GPIO init failed: %s", exc)

    def _on_edge(self, pin: int) -> None:
        """按下回调（GPIO 线程）"""
        if self._pressed_cb:
            try:
                self._pressed_cb()
            except Exception as exc:
                logger.error("Button press callback error: %s", exc)

    def _on_release_edge(self, pin: int) -> None:
        """松开回调（GPIO 线程）"""
        if self._released_cb:
            try:
                self._released_cb()
            except Exception as exc:
                logger.error("Button release callback error: %s", exc)

    def is_pressed(self) -> bool:
        """当前按钮状态"""
        if not _GPIO_AVAILABLE:
            return False
        try:
            val = GPIO.input(self.pin)
            return (not val) if self.active_low else val
        except Exception:
            return False

    def stop(self) -> None:
        """释放 GPIO 资源"""
        self._running = False
        if _GPIO_AVAILABLE:
            try:
                GPIO.cleanup(self.pin)
            except Exception:
                pass
        logger.info("Button stopped")
