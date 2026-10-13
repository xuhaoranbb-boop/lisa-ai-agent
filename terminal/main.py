#!/usr/bin/env python3
"""
丽萨终端 - 主程序

Orange Pi 5 Pro 上的丽萨语音终端客户端。
通过 WebSocket 连接 PC 端 bridge，实现完整的语音对话流程。

用法：
  python main.py                    # 使用默认 config.yaml
  python main.py --config my.yaml   # 指定配置文件
  python main.py --selftest         # 硬件自检模式
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import struct
import sys
import time
from pathlib import Path

from lisa_client.audio import AudioEngine
from lisa_client.bridge_client import BridgeClient, BridgeMessage
from lisa_client.button import Button
from lisa_client.config import Config, load_config
from lisa_client.led_ring import LEDRing, LedState
from lisa_client.local_asr import LocalASR

logger = logging.getLogger("lisa")

# ==================== 版本 ====================
VERSION = "1.0.0"


class LisaTerminal:
    """丽萨终端主控"""

    def __init__(self, config: Config):
        self.config = config
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown_event = asyncio.Event()

        # 核心组件
        self.audio = AudioEngine(
            sample_rate=config.audio.sample_rate,
            channels=config.audio.channels,
            input_device=config.audio.input_device,
            output_device=config.audio.output_device,
            block_size=config.audio.block_size,
            silence_threshold=config.audio.silence_threshold,
        )
        self.bridge = BridgeClient(
            ws_url=config.bridge.ws_url,
            device_id=config.bridge.device_id,
            firmware_version=VERSION,
            reconnect_interval=config.bridge.reconnect_interval,
            heartbeat_interval=config.bridge.heartbeat_interval,
        )
        self.led = LEDRing(
            count=config.led.count,
            spi_device=config.led.spi_device,
            brightness=config.led.brightness,
            use_spi=config.led.use_spi,
        )
        self.button = Button(
            pin=config.gpio.button_pin,
            active_low=config.gpio.button_active_low,
            debounce_ms=config.gpio.button_debounce_ms,
        )
        self.local_asr = LocalASR(
            model_size=config.local_asr.model_size,
            language=config.local_asr.language,
            vad_filter=config.local_asr.vad_filter,
            beam_size=config.local_asr.beam_size,
        ) if config.local_asr.enabled else None

        # 状态
        self._recording = False
        self._audio_queue: asyncio.Queue[bytes] | None = None
        self._record_task: asyncio.Task | None = None

    async def run(self) -> None:
        """启动终端"""
        self._loop = asyncio.get_running_loop()

        # 初始化硬件
        self._init_hardware()

        # 注册 bridge 回调
        self.bridge.on_message(self._on_bridge_message)
        self.bridge.on_connected(self._on_bridge_connected)
        self.bridge.on_disconnected(self._on_bridge_disconnected)

        # 加载离线 ASR（后台）
        if self.local_asr:
            asyncio.create_task(self._load_local_asr())

        # 启动按钮监听
        self.button.on_press(lambda: asyncio.run_coroutine_threadsafe(
            self._on_button_press(), self._loop  # type: ignore
        ))
        self.button.on_release(lambda: asyncio.run_coroutine_threadsafe(
            self._on_button_release(), self._loop  # type: ignore
        ))
        self.button.start()

        # 显示启动画面
        self.led.set_state(LedState.IDLE)
        logger.info("=" * 50)
        logger.info("丽萨终端 v%s 启动完成", VERSION)
        logger.info("设备 ID: %s", self.config.bridge.device_id)
        logger.info("Bridge: %s:%d", self.config.bridge.host, self.config.bridge.port)
        logger.info("按按钮开始说话（PTT 模式）")
        logger.info("=" * 50)

        # 启动 bridge 连接（自动重连）
        try:
            await asyncio.gather(
                self.bridge.connect(),
                self._wait_shutdown(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            self._cleanup()

    def _init_hardware(self) -> None:
        """初始化硬件外设"""
        logger.info("Initializing hardware...")

        # LED 灯环
        self.led.start()
        self.led.set_state(LedState.IDLE)

        # 列出音频设备（调试）
        self.audio.list_devices()

        # 播放启动提示音
        self.audio.play_tone(freq=880, duration=0.1, volume=0.2)

        logger.info("Hardware initialized")

    async def _load_local_asr(self) -> None:
        """后台加载离线 ASR 模型"""
        if self.local_asr:
            ok = await self.local_asr.load()
            if ok:
                logger.info("Offline ASR ready")
            else:
                logger.warning("Offline ASR failed to load — will rely on bridge only")

    # ==================== 按钮事件 ====================

    async def _on_button_press(self) -> None:
        """按钮按下 → 开始录音"""
        if self._recording:
            return
        self._recording = True
        logger.info("Button pressed — recording started")

        # 提示音
        self.audio.play_tone(freq=660, duration=0.08, volume=0.2)

        # 灯环 → 红色
        self.led.set_state(LedState.RECORDING)

        if self.bridge.connected:
            # 在线模式：通知 bridge 开始录音
            await self.bridge.send_button_pressed()
            # 开始录音，音频数据发给 bridge
            self._audio_queue = self.audio.start_recording(self._loop)  # type: ignore
            self._record_task = asyncio.create_task(self._stream_audio_to_bridge())
        else:
            # 离线模式：录音到本地缓冲区
            self._audio_queue = self.audio.start_recording(self._loop)  # type: ignore
            logger.info("Bridge offline — recording locally for offline ASR")

    async def _on_button_release(self) -> None:
        """按钮松开 → 停止录音，处理结果"""
        if not self._recording:
            return
        self._recording = False
        logger.info("Button released — recording stopped")

        # 提示音
        self.audio.play_tone(freq=440, duration=0.08, volume=0.2)

        # 停止录音
        self.audio.stop_recording()
        if self._record_task:
            self._record_task.cancel()
            self._record_task = None

        # 收集录音数据
        pcm_chunks: list[bytes] = []
        if self._audio_queue:
            while not self._audio_queue.empty():
                pcm_chunks.append(self._audio_queue.get_nowait())

        raw_pcm = b"".join(pcm_chunks)
        if not raw_pcm or len(raw_pcm) < 3200:  # < 100ms 的录音忽略
            logger.info("Recording too short, ignored")
            self.led.set_state(LedState.IDLE)
            return

        if self.bridge.connected:
            # 在线模式：通知 bridge 录音结束
            await self.bridge.send_button_released()
            self.led.set_state(LedState.THINKING)
        else:
            # 离线模式：用本地 ASR
            await self._handle_offline(raw_pcm)

    async def _stream_audio_to_bridge(self) -> None:
        """录音期间实时把音频数据发给 bridge"""
        assert self._audio_queue is not None
        try:
            while self._recording:
                try:
                    pcm = await asyncio.wait_for(self._audio_queue.get(), timeout=0.5)
                    await self.bridge.send_audio(pcm)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break
        except Exception as exc:
            logger.warning("Audio streaming error: %s", exc)

    async def _handle_offline(self, pcm_data: bytes) -> None:
        """离线模式：本地 ASR + 简单回复"""
        self.led.set_state(LedState.THINKING)

        if not self.local_asr:
            logger.warning("No offline ASR available")
            self.led.set_state(LedState.ERROR)
            await asyncio.sleep(1)
            self.led.set_state(LedState.OFFLINE)
            return

        # 转写
        text = await self.local_asr.transcribe(pcm_data)
        if not text:
            logger.info("Offline ASR: no result")
            self.led.set_state(LedState.IDLE)
            return

        logger.info("Offline transcript: %s", text)

        # 简单离线回复（没有 LLM，只能做基本应答）
        reply = self._simple_offline_reply(text)
        logger.info("Offline reply: %s", reply)

        # 用 TTS 播放回复（如果有离线 TTS）
        # 目前离线模式只打日志 + 显示文本
        self.led.set_state(LedState.SPEAKING)
        logger.info("[OFFLINE] 丽萨: %s", reply)
        await asyncio.sleep(2)
        self.led.set_state(LedState.OFFLINE)

    def _simple_offline_reply(self, text: str) -> str:
        """离线简单回复（无 LLM 时的兜底）"""
        text_lower = text.lower()
        if "时间" in text or "几点" in text:
            return f"现在是 {time.strftime('%H:%M')}"
        if "你好" in text or "嗨" in text:
            return "你好！我是丽萨，网络好像断了，但我还在。"
        if "天气" in text:
            return "网络断了，查不了天气。连上网就好了。"
        return f"你说的是：{text}。网络断了，我只能听到你说话，但没法帮你做事。"

    # ==================== Bridge 消息处理 ====================

    async def _on_bridge_message(self, msg: BridgeMessage) -> None:
        """处理 bridge 下发的消息"""
        if msg.type == "display":
            logger.info("[Display] %s: %s", msg.status, msg.text[:40])
            # TODO: 推送到 OLED 屏幕

        elif msg.type == "led":
            self.led.set_from_bridge(msg.r, msg.g, msg.b, msg.mode)

        elif msg.type == "audio":
            # 收到 WAV 音频 → 播放
            self.led.set_state(LedState.SPEAKING)
            await self.audio.play_wav(msg.audio_data)
            # 播放完毕恢复待机
            if not self._recording:
                self.led.set_state(LedState.IDLE)

        elif msg.type == "error":
            logger.error("Bridge error: %s", msg.text)
            self.led.set_state(LedState.ERROR)
            await asyncio.sleep(2)
            if not self._recording:
                self.led.set_state(LedState.IDLE)

    async def _on_bridge_connected(self) -> None:
        """Bridge 连接成功"""
        logger.info("Bridge connected — online mode active")
        self.led.set_state(LedState.IDLE)

    def _on_bridge_disconnected(self) -> None:
        """Bridge 断连"""
        logger.warning("Bridge disconnected — will auto-reconnect")
        if not self._recording:
            self.led.set_state(LedState.OFFLINE)

    # ==================== 生命周期 ====================

    async def _wait_shutdown(self) -> None:
        """等待关闭信号"""
        await self._shutdown_event.wait()

    def _cleanup(self) -> None:
        """清理所有资源"""
        logger.info("Shutting down...")
        self.audio.cleanup()
        self.led.cleanup()
        self.button.stop()
        if self.local_asr:
            self.local_asr.unload()
        logger.info("Goodbye!")

    def shutdown(self) -> None:
        """触发关闭"""
        self._shutdown_event.set()


# ==================== 自检模式 ====================

async def selftest(config: Config) -> None:
    """硬件自检"""
    print("=" * 50)
    print("丽萨终端 硬件自检")
    print("=" * 50)

    # 1. LED
    print("\n[1/4] LED 灯环...")
    led = LEDRing(
        count=config.led.count,
        spi_device=config.led.spi_device,
        brightness=config.led.brightness,
        use_spi=config.led.use_spi,
    )
    led.start()
    for name, color in [
        ("红色", (255, 0, 0)),
        ("绿色", (0, 255, 0)),
        ("蓝色", (0, 0, 255)),
        ("白色", (255, 255, 255)),
    ]:
        r, g, b = color
        led.set_color(r, g, b)
        print(f"  {name} (R={r} G={g} B={b}) — 按回车继续")
        await asyncio.get_event_loop().run_in_executor(None, input)
    led.off()
    led.cleanup()
    print("  LED OK")

    # 2. 音频播放
    print("\n[2/4] 扬声器...")
    audio = AudioEngine(
        sample_rate=config.audio.sample_rate,
        output_device=config.audio.output_device,
    )
    audio.list_devices()
    audio.play_tone(freq=440, duration=0.5, volume=0.3)
    print("  应该听到了 440Hz 提示音。按回车继续")
    await asyncio.get_event_loop().run_in_executor(None, input)
    print("  Speaker OK")

    # 3. 麦克风
    print("\n[3/4] 麦克风（录 3 秒）...")
    audio_in = AudioEngine(
        sample_rate=config.audio.sample_rate,
        input_device=config.audio.input_device,
    )
    audio_in.list_devices()
    loop = asyncio.get_event_loop()
    queue = audio_in.start_recording(loop)
    print("  请说话... 3 秒")
    await asyncio.sleep(3)
    audio_in.stop_recording()
    chunks = []
    while not queue.empty():
        chunks.append(queue.get_nowait())
    total = sum(len(c) for c in chunks)
    duration = total / (config.audio.sample_rate * 2)
    print(f"  录到 {total} bytes ({duration:.1f}s)")
    print("  Microphone OK" if total > 0 else "  WARNING: 没有录到数据！")

    # 4. 按钮
    print("\n[4/4] 按钮...")
    print("  按下按钮后 3 秒内松开即可")
    btn = Button(pin=config.gpio.button_pin, active_low=config.gpio.button_active_low)
    btn.start()
    for i in range(30):
        if btn.is_pressed():
            print("  检测到按钮按下！")
            break
        await asyncio.sleep(0.1)
    else:
        print("  未检测到按钮（可能接线有误，跳过）")
    btn.stop()

    audio.cleanup()
    audio_in.cleanup()
    print("\n" + "=" * 50)
    print("自检完成！")
    print("=" * 50)


# ==================== 入口 ====================

def main() -> None:
    parser = argparse.ArgumentParser(description="丽萨终端 - Orange Pi 5 Pro")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--selftest", action="store_true", help="硬件自检模式")
    parser.add_argument("--debug", action="store_true", help="调试日志")
    args = parser.parse_args()

    # 加载配置
    config = load_config(args.config)
    if args.debug:
        config.log_level = "DEBUG"

    # 日志
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if config.log_file:
        try:
            handlers.append(logging.FileHandler(config.log_file, encoding="utf-8"))
        except Exception:
            pass
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )

    if args.selftest:
        asyncio.run(selftest(config))
        return

    # 主程序
    terminal = LisaTerminal(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # 信号处理
    def _signal_handler() -> None:
        logger.info("Signal received, shutting down...")
        terminal.shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        loop.run_until_complete(terminal.run())
    except KeyboardInterrupt:
        terminal.shutdown()
    finally:
        loop.close()


if __name__ == "__main__":
    main()
