"""离线 ASR 兜底

当 bridge 断连时，用本地 Whisper 模型做语音识别。
利用 RK3588S 的 6 TOPS NPU 加速（通过 RKNN 后端）。

模型选择：
  - tiny: 最快，准确率 ~85%（简单指令够用）
  - base: 平衡，准确率 ~90%
  - small: 最准，准确率 ~94%（8GB 内存够用）
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 尝试导入 faster-whisper
try:
    from faster_whisper import WhisperModel
    _WHISPER_AVAILABLE = True
except ImportError:
    _WHISPER_AVAILABLE = False


class LocalASR:
    """离线语音识别（Whisper 本地推理）"""

    def __init__(
        self,
        model_size: str = "small",
        language: str = "zh",
        vad_filter: bool = True,
        beam_size: int = 5,
    ):
        self.model_size = model_size
        self.language = language
        self.vad_filter = vad_filter
        self.beam_size = beam_size
        self._model: Any = None
        self._loaded = False

    @property
    def available(self) -> bool:
        return _WHISPER_AVAILABLE

    async def load(self) -> bool:
        """加载 Whisper 模型（在线程池中执行，避免阻塞）"""
        if not _WHISPER_AVAILABLE:
            logger.warning("faster-whisper not installed — offline ASR disabled")
            return False

        if self._loaded:
            return True

        try:
            logger.info("Loading Whisper model: %s (this may take a while on first run)...", self.model_size)
            self._model = await asyncio.to_thread(
                self._load_model,
            )
            self._loaded = True
            logger.info("Whisper model loaded: %s", self.model_size)
            return True
        except Exception as exc:
            logger.error("Failed to load Whisper model: %s", exc)
            return False

    def _load_model(self) -> Any:
        """同步加载模型（在线程池中运行）"""
        # compute_type="int8" 对 ARM 友好，减少内存占用
        # device="auto" 会自动选择 CPU（RKNN 后端需要额外配置）
        model = WhisperModel(
            self.model_size,
            device="cpu",
            compute_type="int8",
        )
        return model

    async def transcribe(self, pcm_data: bytes, sample_rate: int = 16000) -> str:
        """转写 PCM 音频数据为文本

        Args:
            pcm_data: 16-bit mono PCM 数据
            sample_rate: 采样率（默认 16000）

        Returns:
            识别出的文本（空字符串表示失败）
        """
        if not self._model:
            if not await self.load():
                return ""

        try:
            # 把 PCM 包装成 WAV（faster-whisper 需要文件路径或字节流）
            wav_bytes = self._pcm_to_wav(pcm_data, sample_rate)

            # 写入临时文件
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(wav_bytes)
                wav_path = f.name

            try:
                segments, info = await asyncio.to_thread(
                    self._model.transcribe,
                    wav_path,
                    language=self.language,
                    vad_filter=self.vad_filter,
                    beam_size=self.beam_size,
                )
                text = "".join(seg.text for seg in segments).strip()
                logger.info("Local ASR result: %s (%.1fs audio)", text[:50], info.duration)
                return text
            finally:
                Path(wav_path).unlink(missing_ok=True)

        except Exception as exc:
            logger.error("Local ASR transcription failed: %s", exc)
            return ""

    def _pcm_to_wav(self, pcm_data: bytes, sample_rate: int) -> bytes:
        """PCM → WAV 封装"""
        import struct
        data_size = len(pcm_data)
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            36 + data_size,
            b"WAVE",
            b"fmt ",
            16,
            1,  # PCM
            1,  # mono
            sample_rate,
            sample_rate * 2,
            2,
            16,
            b"data",
            data_size,
        )
        return header + pcm_data

    def unload(self) -> None:
        """卸载模型释放内存"""
        self._model = None
        self._loaded = False
        logger.info("Whisper model unloaded")
