"""音频采集与播放

使用 sounddevice 做麦克风采集和扬声器播放。
录音：16kHz 16-bit mono PCM
播放：接收 WAV 格式直接播放
"""

from __future__ import annotations

import asyncio
import io
import logging
import struct
import wave
from typing import Any, Callable

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


class AudioEngine:
    """麦克风采集 + 扬声器播放"""

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        input_device: int | str | None = None,
        output_device: int | str | None = None,
        block_size: int = 1024,
        silence_threshold: int = 300,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.input_device = input_device
        self.output_device = output_device
        self.block_size = block_size
        self.silence_threshold = silence_threshold

        self._recording = False
        self._audio_queue: asyncio.Queue[bytes] | None = None
        self._stream: sd.InputStream | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def list_devices(self) -> None:
        """列出所有音频设备（调试用）"""
        logger.info("Audio devices:\n%s", sd.query_devices())

    def start_recording(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue[bytes]:
        """开始录音，返回 PCM 数据队列

        每次回调产生一块 PCM 数据（bytes），放入队列。
        调用 stop_recording() 停止。
        """
        if self._recording:
            logger.warning("Already recording")
            return self._audio_queue  # type: ignore

        self._loop = loop
        self._audio_queue = asyncio.Queue()
        self._recording = True

        def _audio_callback(indata: np.ndarray, frames: int, time_info: Any, status: sd.CallbackFlags) -> None:
            if status:
                logger.warning("Audio input status: %s", status)
            if not self._recording:
                return
            # float32 → int16 PCM
            pcm = (indata * 32767).astype(np.int16).tobytes()
            # 线程安全地丢进 asyncio 队列
            if self._loop and self._audio_queue:
                self._loop.call_soon_threadsafe(self._audio_queue.put_nowait, pcm)

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="float32",
            blocksize=self.block_size,
            device=self.input_device,
            callback=_audio_callback,
        )
        self._stream.start()
        logger.info("Recording started (sr=%d, device=%s)", self.sample_rate, self.input_device)
        return self._audio_queue

    def stop_recording(self) -> None:
        """停止录音"""
        self._recording = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:
                logger.warning("Error stopping recording: %s", exc)
            self._stream = None
        logger.info("Recording stopped")

    def is_silent(self, pcm_data: bytes) -> bool:
        """判断一段 PCM 数据是否接近静音"""
        if len(pcm_data) < 2:
            return True
        samples = np.frombuffer(pcm_data, dtype=np.int16)
        rms = np.sqrt(np.mean(samples.astype(float) ** 2))
        return rms < self.silence_threshold

    async def play_wav(self, wav_data: bytes) -> None:
        """播放 WAV 音频数据（在线程池中执行，不阻塞事件循环）"""
        await asyncio.to_thread(self._play_wav_sync, wav_data)

    def _play_wav_sync(self, wav_data: bytes) -> None:
        """同步播放 WAV（在线程池中运行）"""
        try:
            with wave.open(io.BytesIO(wav_data), "rb") as wf:
                sr = wf.getframerate()
                ch = wf.getnchannels()
                sw = wf.getsampwidth()
                frames = wf.readframes(wf.getnframes())

            if sw == 2:
                dtype = np.int16
            elif sw == 4:
                dtype = np.int32
            else:
                dtype = np.uint8

            audio_array = np.frombuffer(frames, dtype=dtype).astype(np.float32) / (32767 if sw == 2 else 1)
            if ch > 1:
                audio_array = audio_array.reshape(-1, ch)

            sd.play(audio_array, samplerate=sr, device=self.output_device)
            sd.wait()
            logger.info("Audio playback done (%d frames @ %dHz)", len(frames) // (sw * ch), sr)

        except Exception as exc:
            logger.error("Audio playback failed: %s", exc)

    def play_tone(self, freq: int = 440, duration: float = 0.15, volume: float = 0.3) -> None:
        """播放一个短促提示音（哔声）"""
        try:
            t = np.linspace(0, duration, int(self.sample_rate * duration), False)
            tone = np.sin(2 * np.pi * freq * t) * volume
            # 淡入淡出避免爆音
            fade = min(int(0.01 * self.sample_rate), len(tone) // 4)
            if fade > 0:
                tone[:fade] *= np.linspace(0, 1, fade)
                tone[-fade:] *= np.linspace(1, 0, fade)
            sd.play(tone, samplerate=self.sample_rate, device=self.output_device)
            sd.wait()
        except Exception as exc:
            logger.warning("Tone playback failed: %s", exc)

    def cleanup(self) -> None:
        """释放音频资源"""
        self.stop_recording()
        try:
            sd.stop()
        except Exception:
            pass
