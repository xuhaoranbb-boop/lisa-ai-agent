from __future__ import annotations

import asyncio
import re
import threading

import numpy as np
from faster_whisper import WhisperModel

_TRANSCRIPT_CORRECTIONS = {
    "古维斯": "贾维斯",
    "谷维斯": "贾维斯",
    "古为司": "贾维斯",
    "古为斯": "贾维斯",
    "古威斯": "贾维斯",
    "顾维斯": "贾维斯",
    "顾威斯": "贾维斯",
    "加维斯": "贾维斯",
    "加威斯": "贾维斯",
    "家维斯": "贾维斯",
    "家威斯": "贾维斯",
    "甲维斯": "贾维斯",
    "贾威斯": "贾维斯",
    "贾伟斯": "贾维斯",
    "顧為私": "贾维斯",
    "v s code": "VS Code",
    "vs code": "VS Code",
    "vscode": "VS Code",
    "visual studio code": "VS Code",
    "deep seek": "DeepSeek",
    "deepseek": "DeepSeek",
    "欧拉玛": "Ollama",
    "欧拉马": "Ollama",
    "迪普西克": "DeepSeek",
    "迪普希克": "DeepSeek",
    "地铺西克": "DeepSeek",
    "打开v s code": "打开 VS Code",
    "打开vscode": "打开 VS Code",
    "打开visual studio code": "打开 VS Code",
    "刘览器": "浏览器",
    "流览器": "浏览器",
    "打开刘览器": "打开浏览器",
    "打开流览器": "打开浏览器",
    "薇信": "微信",
    "威信": "微信",
    "截屏": "截图",
    "关掉窗口": "关闭窗口",
    "关掉当前窗口": "关闭当前窗口",
    "拍森": "Python",
    "派森": "Python",
    "power shell": "PowerShell",
    "m c p": "MCP",
    "q q": "QQ",
}

_CHINESE_CHAR = r"\u4e00-\u9fff"
_CHINESE_PUNCT = r"，。！？：；、"
_WHISPER_ARTIFACT_PATTERNS = (
    re.compile(r"(?:请不吝点赞|订阅|关注|一键三连|谢谢观看|感谢收看)[，。！？\s]*", re.I),
)
_PROMPT_TERM_PATTERN = re.compile(r"[\u4e00-\u9fffA-Za-z0-9_+.#-]{2,20}")
_SECRET_LIKE_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{6,}|api[_-]?key|password|passwd|secret|token)",
    re.I,
)


def normalize_transcript(text: str) -> str:
    normalized = text.strip()
    for heard, intended in _TRANSCRIPT_CORRECTIONS.items():
        normalized = normalized.replace(heard, intended)
    normalized = re.sub(
        rf"(?<=[{_CHINESE_CHAR}])\s+(?=[{_CHINESE_CHAR}])",
        "",
        normalized,
    )
    normalized = re.sub(rf"\s+([{_CHINESE_PUNCT}])", r"\1", normalized)
    normalized = re.sub(rf"([{_CHINESE_PUNCT}])\s+", r"\1", normalized)
    normalized = re.sub(r"[ \t]+", " ", normalized).strip()
    for pattern in _WHISPER_ARTIFACT_PATTERNS:
        normalized = pattern.sub("", normalized).strip()
    return normalized


def build_dynamic_initial_prompt(
    base_prompt: str,
    memory_texts: list[str] | tuple[str, ...],
    *,
    max_terms: int = 24,
    max_chars: int = 800,
) -> str:
    """Bias Whisper toward the user's recurring names, apps, and projects.

    Whisper only attends to the tail of an over-long prompt, so the static
    domain prompt stays first and memory-derived terms are de-duplicated,
    capped, and stripped of anything that looks like a credential.
    """
    base = base_prompt.strip()
    terms: list[str] = []
    seen: set[str] = set()
    for text in memory_texts:
        for term in _PROMPT_TERM_PATTERN.findall(str(text)):
            if _SECRET_LIKE_PATTERN.search(term):
                continue
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            terms.append(term)
            if len(terms) >= max_terms:
                break
        if len(terms) >= max_terms:
            break
    if not terms:
        return base[:max_chars]
    suffix = " 用户常用词：" + "、".join(terms) + "。"
    return (base + suffix)[:max_chars]


class StreamingWhisperASR:
    """Rolling-buffer Whisper transcriber for continuous PCM16 microphone input."""

    def __init__(
        self,
        model: WhisperModel,
        *,
        partial_model: WhisperModel | None = None,
        sample_rate: int = 16_000,
        max_buffer_seconds: float = 20.0,
        partial_window_seconds: float = 8.0,
        language: str = "zh",
        initial_prompt: str = (
            "以下是中文语音对话。常用词：贾维斯、Jarvis、DeepSeek、Ollama、"
            "VS Code、打开应用、运行测试、查看文件、记住、继续、停止。"
        ),
        hotwords: str = "",
        no_speech_threshold: float = 0.6,
        final_beam_size: int = 3,
        final_vad_filter: bool = True,
        short_final_seconds: float = 2.5,
        short_final_beam_size: int = 2,
    ) -> None:
        self.model = model
        self.partial_model = partial_model or model
        self.sample_rate = sample_rate
        self.max_buffer_bytes = int(max_buffer_seconds * sample_rate * 2)
        self.partial_window_bytes = int(partial_window_seconds * sample_rate * 2)
        self.language = language
        self.initial_prompt = initial_prompt
        self.hotwords = hotwords
        self.no_speech_threshold = no_speech_threshold
        self.final_beam_size = final_beam_size
        self.final_vad_filter = final_vad_filter
        self.short_final_seconds = max(0.5, short_final_seconds)
        self.short_final_beam_size = max(1, short_final_beam_size)
        self._buffer = bytearray()
        self._lock = threading.Lock()

    def feed(self, pcm_bytes: bytes) -> None:
        with self._lock:
            self._buffer.extend(pcm_bytes)
            if len(self._buffer) > self.max_buffer_bytes:
                del self._buffer[: len(self._buffer) - self.max_buffer_bytes]

    def reset(self) -> None:
        with self._lock:
            self._buffer.clear()

    async def partial(self) -> str:
        return await asyncio.to_thread(self._transcribe, self.partial_window_bytes)

    async def final(self) -> str:
        with self._lock:
            duration_seconds = len(self._buffer) / 2 / self.sample_rate
        return await asyncio.to_thread(
            self._transcribe,
            len(self._buffer),
            final=True,
            fast_final=duration_seconds <= self.short_final_seconds,
        )

    def _transcribe(
        self,
        window_bytes: int,
        *,
        final: bool = False,
        fast_final: bool = False,
    ) -> str:
        with self._lock:
            data = bytes(self._buffer[-window_bytes:])
        if len(data) < self.sample_rate:
            return ""
        audio = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        transcribe_model = (
            self.partial_model
            if fast_final or not final
            else self.model
        )
        if final and fast_final:
            beam_size = self.short_final_beam_size
        elif final:
            beam_size = self.final_beam_size
        else:
            beam_size = 1
        use_vad_filter = self.final_vad_filter if final and not fast_final else False
        transcribe_options: dict[str, object] = {}
        if self.hotwords.strip():
            transcribe_options["hotwords"] = self.hotwords.strip()
        segments, _ = transcribe_model.transcribe(
            audio,
            language=self.language,
            beam_size=beam_size,
            temperature=0.0,
            vad_filter=use_vad_filter,
            condition_on_previous_text=False,
            without_timestamps=True,
            initial_prompt=self.initial_prompt,
            no_speech_threshold=self.no_speech_threshold,
            **transcribe_options,
        )
        return normalize_transcript("".join(segment.text for segment in segments))
