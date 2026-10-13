from __future__ import annotations

import asyncio
import importlib
import importlib.util
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from jarvis.config import Settings
from jarvis.streaming_asr import StreamingWhisperASR, normalize_transcript

_SHERPA_RECOGNIZER_CACHE: dict[tuple[str, int, str], Any] = {}
_SHERPA_SENSEVOICE_CACHE: dict[tuple[str, int, str, str], Any] = {}


class AsrProviderUnavailable(RuntimeError):
    """Raised when an optional ASR backend is not installed or configured."""


@dataclass(frozen=True)
class AsrEvent:
    provider: str
    session_id: str
    request_id: str
    partial_text: str
    final_text: str
    is_final: bool
    confidence: float | None
    language: str
    start_ms: float
    end_ms: float


class AsrProvider(Protocol):
    name: str

    async def start_session(
        self,
        *,
        session_id: str = "",
        request_id: str = "",
    ) -> AsrEvent: ...

    def push_audio(self, chunk: bytes) -> None: ...

    async def partial(self) -> AsrEvent: ...

    async def flush(self) -> AsrEvent: ...

    async def cancel(self) -> None: ...

    async def close(self) -> None: ...


class WhisperAsrProvider:
    """Streaming provider backed by faster-whisper."""

    name = "whisper"

    def __init__(
        self,
        model: Any,
        *,
        partial_model: Any | None = None,
        partial_window_seconds: float = 8.0,
        initial_prompt: str = "",
        hotwords: str = "",
        no_speech_threshold: float = 0.6,
        final_beam_size: int = 2,
        final_vad_filter: bool = True,
        short_final_seconds: float = 2.5,
        short_final_beam_size: int = 2,
        language: str = "zh",
    ) -> None:
        self._transcriber = StreamingWhisperASR(
            model,
            partial_model=partial_model,
            partial_window_seconds=partial_window_seconds,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            no_speech_threshold=no_speech_threshold,
            final_beam_size=final_beam_size,
            final_vad_filter=final_vad_filter,
            short_final_seconds=short_final_seconds,
            short_final_beam_size=short_final_beam_size,
            language=language,
        )
        self._session_id = ""
        self._request_id = ""
        self._started_at = 0.0
        self._language = language

    async def start_session(
        self,
        *,
        session_id: str = "",
        request_id: str = "",
    ) -> AsrEvent:
        self._transcriber.reset()
        self._session_id = session_id
        self._request_id = request_id
        self._started_at = time.perf_counter()
        return self._event()

    def push_audio(self, chunk: bytes) -> None:
        self._transcriber.feed(chunk)

    async def partial(self) -> AsrEvent:
        text = await self._transcriber.partial()
        return self._event(partial_text=text)

    async def flush(self) -> AsrEvent:
        text = await self._transcriber.final()
        return self._event(final_text=text, is_final=True)

    async def cancel(self) -> None:
        self._transcriber.reset()

    async def close(self) -> None:
        self._transcriber.reset()

    def _event(
        self,
        *,
        partial_text: str = "",
        final_text: str = "",
        is_final: bool = False,
    ) -> AsrEvent:
        return AsrEvent(
            provider=self.name,
            session_id=self._session_id,
            request_id=self._request_id,
            partial_text=partial_text,
            final_text=final_text,
            is_final=is_final,
            confidence=None,
            language=self._language,
            start_ms=0.0,
            end_ms=round((time.perf_counter() - self._started_at) * 1_000, 3),
        )


class SherpaOnnxAsrProvider:
    """Streaming Paraformer provider backed by sherpa-onnx."""

    name = "paraformer-zh-streaming"

    def __init__(
        self,
        model_dir: str,
        *,
        num_threads: int = 2,
        provider: str = "cpu",
        sample_rate: int = 16_000,
    ) -> None:
        try:
            sherpa_onnx = importlib.import_module("sherpa_onnx")
            numpy = importlib.import_module("numpy")
        except ImportError as exc:
            raise AsrProviderUnavailable(
                "Paraformer streaming requires the optional sherpa-onnx package."
            ) from exc
        model_path = Path(model_dir).expanduser()
        encoder = model_path / "encoder.int8.onnx"
        decoder = model_path / "decoder.int8.onnx"
        tokens = model_path / "tokens.txt"
        if not encoder.exists() or not decoder.exists() or not tokens.exists():
            raise AsrProviderUnavailable(
                f"Sherpa ONNX Paraformer model files were not found in {model_path}."
            )
        cache_key = (str(model_path), max(1, int(num_threads)), provider or "cpu")
        cached = _SHERPA_RECOGNIZER_CACHE.get(cache_key)
        if cached is not None:
            self._recognizer = cached
        else:
            try:
                self._recognizer = sherpa_onnx.OnlineRecognizer.from_paraformer(
                    tokens=str(tokens),
                    encoder=str(encoder),
                    decoder=str(decoder),
                    num_threads=cache_key[1],
                    sample_rate=sample_rate,
                    feature_dim=80,
                    enable_endpoint_detection=False,
                    decoding_method="greedy_search",
                    provider=cache_key[2],
                )
            except Exception as exc:
                raise AsrProviderUnavailable(
                    f"Sherpa ONNX Paraformer model could not be loaded: {exc}"
                ) from exc
            _SHERPA_RECOGNIZER_CACHE[cache_key] = self._recognizer
        self._np = numpy
        self._sample_rate = sample_rate
        self._stream: Any | None = None
        self._buffer = bytearray()
        self._fed_bytes = 0
        self._session_id = ""
        self._request_id = ""
        self._started_at = 0.0
        self._language = "zh-en"

    async def start_session(
        self,
        *,
        session_id: str = "",
        request_id: str = "",
    ) -> AsrEvent:
        self._stream = self._recognizer.create_stream()
        self._buffer.clear()
        self._fed_bytes = 0
        self._session_id = session_id
        self._request_id = request_id
        self._started_at = time.perf_counter()
        return self._event()

    def push_audio(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)

    async def partial(self) -> AsrEvent:
        text = await asyncio.to_thread(self._decode_available)
        return self._event(partial_text=normalize_transcript(text))

    async def flush(self) -> AsrEvent:
        text = await asyncio.to_thread(self._decode_available, True)
        return self._event(final_text=normalize_transcript(text), is_final=True)

    async def cancel(self) -> None:
        self._stream = None
        self._buffer.clear()
        self._fed_bytes = 0

    async def close(self) -> None:
        await self.cancel()

    def _decode_available(self, final: bool = False) -> str:
        stream = self._stream
        if stream is None:
            return ""
        if self._fed_bytes < len(self._buffer):
            data = bytes(self._buffer[self._fed_bytes :])
            samples = (
                self._np.frombuffer(data, dtype="<i2").astype(self._np.float32)
                / 32768.0
            )
            stream.accept_waveform(self._sample_rate, samples)
            self._fed_bytes = len(self._buffer)
        if final:
            stream.input_finished()
        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)
        return str(self._recognizer.get_result(stream) or "")

    def _event(
        self,
        *,
        partial_text: str = "",
        final_text: str = "",
        is_final: bool = False,
    ) -> AsrEvent:
        return AsrEvent(
            provider=self.name,
            session_id=self._session_id,
            request_id=self._request_id,
            partial_text=partial_text,
            final_text=final_text,
            is_final=is_final,
            confidence=None,
            language=self._language,
            start_ms=0.0,
            end_ms=round((time.perf_counter() - self._started_at) * 1_000, 3),
        )


class SherpaOnnxSenseVoiceProvider:
    """Offline SenseVoice provider used as the high-accuracy final recognizer."""

    name = "sensevoice"

    def __init__(
        self,
        model_dir: str,
        *,
        num_threads: int = 2,
        provider: str = "cpu",
        language: str = "auto",
        sample_rate: int = 16_000,
        min_partial_seconds: float = 0.4,
    ) -> None:
        try:
            sherpa_onnx = importlib.import_module("sherpa_onnx")
            numpy = importlib.import_module("numpy")
        except ImportError as exc:
            raise AsrProviderUnavailable(
                "SenseVoice requires the optional sherpa-onnx package."
            ) from exc
        model_path = Path(model_dir).expanduser()
        model = model_path / "model.int8.onnx"
        tokens = model_path / "tokens.txt"
        if not model.exists() or not tokens.exists():
            raise AsrProviderUnavailable(
                f"SenseVoice model files were not found in {model_path}."
            )
        cache_key = (
            str(model_path),
            max(1, int(num_threads)),
            provider or "cpu",
            language or "auto",
        )
        cached = _SHERPA_SENSEVOICE_CACHE.get(cache_key)
        if cached is not None:
            self._recognizer = cached
        else:
            try:
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    tokens=str(tokens),
                    model=str(model),
                    num_threads=cache_key[1],
                    sample_rate=sample_rate,
                    feature_dim=80,
                    decoding_method="greedy_search",
                    provider=cache_key[2],
                    language=cache_key[3],
                    use_itn=True,
                )
            except Exception as exc:
                raise AsrProviderUnavailable(
                    f"SenseVoice model could not be loaded: {exc}"
                ) from exc
            _SHERPA_SENSEVOICE_CACHE[cache_key] = self._recognizer
        self._np = numpy
        self._sample_rate = sample_rate
        self._min_partial_bytes = int(sample_rate * 2 * max(0.1, min_partial_seconds))
        self._buffer = bytearray()
        self._last_decoded_bytes = 0
        self._last_text = ""
        self._session_id = ""
        self._request_id = ""
        self._started_at = 0.0
        self._language = language or "auto"

    async def start_session(
        self,
        *,
        session_id: str = "",
        request_id: str = "",
    ) -> AsrEvent:
        self._buffer.clear()
        self._last_decoded_bytes = 0
        self._last_text = ""
        self._session_id = session_id
        self._request_id = request_id
        self._started_at = time.perf_counter()
        return self._event()

    def push_audio(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)

    async def partial(self) -> AsrEvent:
        if len(self._buffer) - self._last_decoded_bytes < self._min_partial_bytes:
            return self._event(partial_text=self._last_text)
        text = await asyncio.to_thread(self._decode)
        self._last_decoded_bytes = len(self._buffer)
        self._last_text = normalize_transcript(text)
        return self._event(partial_text=self._last_text)

    async def flush(self) -> AsrEvent:
        text = await asyncio.to_thread(self._decode)
        self._last_decoded_bytes = len(self._buffer)
        self._last_text = normalize_transcript(text)
        return self._event(final_text=self._last_text, is_final=True)

    async def cancel(self) -> None:
        self._buffer.clear()
        self._last_decoded_bytes = 0
        self._last_text = ""

    async def close(self) -> None:
        await self.cancel()

    def _decode(self) -> str:
        if not self._buffer:
            return ""
        stream = self._recognizer.create_stream()
        samples = (
            self._np.frombuffer(bytes(self._buffer), dtype="<i2").astype(
                self._np.float32
            )
            / 32768.0
        )
        stream.accept_waveform(self._sample_rate, samples)
        self._recognizer.decode_stream(stream)
        return str(stream.result.text or "")

    def _event(
        self,
        *,
        partial_text: str = "",
        final_text: str = "",
        is_final: bool = False,
    ) -> AsrEvent:
        return AsrEvent(
            provider=self.name,
            session_id=self._session_id,
            request_id=self._request_id,
            partial_text=partial_text,
            final_text=final_text,
            is_final=is_final,
            confidence=None,
            language=self._language,
            start_ms=0.0,
            end_ms=round((time.perf_counter() - self._started_at) * 1_000, 3),
        )


class HybridAsrProvider:
    """Fast streaming partials plus a higher-accuracy final decode."""

    name = "paraformer+sensevoice"

    def __init__(
        self,
        partial_provider: AsrProvider,
        final_provider: AsrProvider,
    ) -> None:
        self._partial_provider = partial_provider
        self._final_provider = final_provider

    async def start_session(
        self,
        *,
        session_id: str = "",
        request_id: str = "",
    ) -> AsrEvent:
        partial_event = await self._partial_provider.start_session(
            session_id=session_id,
            request_id=request_id,
        )
        await self._final_provider.start_session(
            session_id=session_id,
            request_id=request_id,
        )
        return replace(partial_event, provider=self.name)

    def push_audio(self, chunk: bytes) -> None:
        self._partial_provider.push_audio(chunk)
        self._final_provider.push_audio(chunk)

    async def partial(self) -> AsrEvent:
        event = await self._partial_provider.partial()
        return replace(event, provider=self.name)

    async def flush(self) -> AsrEvent:
        event = await self._final_provider.flush()
        return replace(event, provider=self.name)

    async def cancel(self) -> None:
        await self._partial_provider.cancel()
        await self._final_provider.cancel()

    async def close(self) -> None:
        await self._partial_provider.close()
        await self._final_provider.close()


class FunAsrProvider:
    """Optional FunASR adapter for Paraformer and SenseVoice."""

    def __init__(
        self,
        provider_name: str,
        model_name: str,
        *,
        streaming: bool,
        sample_rate: int = 16_000,
        partial_window_seconds: float = 8.0,
    ) -> None:
        try:
            funasr = importlib.import_module("funasr")
            numpy = importlib.import_module("numpy")
        except ImportError as exc:
            raise AsrProviderUnavailable(
                f"{provider_name} requires the optional funasr package."
            ) from exc
        self.name = provider_name
        self._np = numpy
        self._streaming = streaming
        self._sample_rate = sample_rate
        self._buffer = bytearray()
        self._partial_window_bytes = int(partial_window_seconds * sample_rate * 2)
        self._cache: dict[str, Any] = {}
        self._last_text = ""
        self._session_id = ""
        self._request_id = ""
        self._started_at = 0.0
        self._language = "zh"
        try:
            self._model = funasr.AutoModel(
                model=model_name,
                disable_update=True,
                trust_remote_code=True,
            )
        except Exception as exc:
            raise AsrProviderUnavailable(
                f"{provider_name} model could not be loaded: {exc}"
            ) from exc

    async def start_session(
        self,
        *,
        session_id: str = "",
        request_id: str = "",
    ) -> AsrEvent:
        self._buffer.clear()
        self._cache.clear()
        self._last_text = ""
        self._session_id = session_id
        self._request_id = request_id
        self._started_at = time.perf_counter()
        return self._event()

    def push_audio(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)

    async def partial(self) -> AsrEvent:
        if not self._streaming:
            return self._event(partial_text=self._last_text)
        audio = self._pcm_to_float(self._buffer[-self._partial_window_bytes :])
        if audio.size == 0:
            return self._event()
        result = await asyncio.to_thread(
            self._model.generate,
            input=audio,
            cache=self._cache,
            is_final=False,
            chunk_size=[0, 10, 5],
            encoder_chunk_look_back=4,
            decoder_chunk_look_back=1,
        )
        text = normalize_transcript(self._extract_text(result))
        self._last_text = text
        return self._event(partial_text=text)

    async def flush(self) -> AsrEvent:
        audio = self._pcm_to_float(self._buffer)
        if audio.size == 0:
            return self._event(is_final=True)
        if self._streaming:
            result = await asyncio.to_thread(
                self._model.generate,
                input=audio,
                cache=self._cache,
                is_final=True,
                chunk_size=[0, 10, 5],
                encoder_chunk_look_back=4,
                decoder_chunk_look_back=1,
            )
        else:
            result = await asyncio.to_thread(
                self._model.generate,
                input=audio,
                cache={},
                language="zh",
                use_itn=True,
                batch_size_s=60,
            )
        text = normalize_transcript(self._extract_text(result))
        self._last_text = text
        return self._event(final_text=text, is_final=True)

    async def cancel(self) -> None:
        self._buffer.clear()
        self._cache.clear()
        self._last_text = ""

    async def close(self) -> None:
        await self.cancel()

    def _pcm_to_float(self, data: bytes) -> Any:
        if not data:
            return self._np.zeros(0, dtype=self._np.float32)
        return (
            self._np.frombuffer(bytes(data), dtype="<i2").astype(self._np.float32)
            / 32768.0
        )

    def _extract_text(self, result: Any) -> str:
        if isinstance(result, dict):
            return re.sub(r"<\|.*?\|>", "", str(result.get("text") or "")).strip()
        if isinstance(result, list):
            return "".join(self._extract_text(item) for item in result).strip()
        return str(result or "").strip()

    def _event(
        self,
        *,
        partial_text: str = "",
        final_text: str = "",
        is_final: bool = False,
    ) -> AsrEvent:
        end_ms = round((time.perf_counter() - self._started_at) * 1_000, 3)
        return AsrEvent(
            provider=self.name,
            session_id=self._session_id,
            request_id=self._request_id,
            partial_text=partial_text,
            final_text=final_text,
            is_final=is_final,
            confidence=None,
            language=self._language,
            start_ms=0.0,
            end_ms=end_ms,
        )


def available_asr_providers() -> list[str]:
    providers: list[str] = []
    if importlib.util.find_spec("sherpa_onnx") is not None:
        providers.extend(["paraformer-zh-streaming", "sensevoice"])
    if importlib.util.find_spec("funasr") is not None:
        providers.append("sensevoice")
    providers = list(dict.fromkeys(providers))
    providers.append("whisper")
    return providers


def create_asr_provider(
    settings: Settings,
    whisper: Any,
    whisper_partial: Any | None = None,
    *,
    initial_prompt: str | None = None,
    logger: Any | None = None,
) -> AsrProvider:
    """Create the first configured ASR provider that is actually available."""
    order = [
        item.strip().lower()
        for item in settings.asr_provider_order.split(",")
        if item.strip()
    ]
    wants_paraformer = any(
        name in {"paraformer-zh-streaming", "paraformer"} for name in order
    )
    wants_sensevoice = any(
        name in {"sensevoice", "sensevoice-small"} for name in order
    )
    if wants_paraformer and wants_sensevoice:
        try:
            if logger is not None:
                logger(
                    "ASR hybrid enabled: Paraformer streaming partials "
                    "+ SenseVoice final recognition."
                )
            return HybridAsrProvider(
                SherpaOnnxAsrProvider(
                    settings.asr_sherpa_model_dir,
                    num_threads=settings.asr_sherpa_num_threads,
                    provider=settings.asr_sherpa_provider,
                ),
                SherpaOnnxSenseVoiceProvider(
                    settings.asr_sensevoice_model_dir,
                    num_threads=settings.asr_sensevoice_num_threads,
                    provider=settings.asr_sensevoice_provider,
                    language=settings.asr_sensevoice_language,
                ),
            )
        except AsrProviderUnavailable as exc:
            if logger is not None:
                logger(
                    "ASR hybrid unavailable; falling back to the provider order: "
                    f"{exc}"
                )
    for name in order:
        try:
            if name in {"qwen3-asr", "qwen-asr", "qwen"}:
                raise AsrProviderUnavailable(
                    "Qwen3-ASR streaming adapter is not configured in this build."
                )
            if name in {"paraformer-zh-streaming", "paraformer"}:
                try:
                    return SherpaOnnxAsrProvider(
                        settings.asr_sherpa_model_dir,
                        num_threads=settings.asr_sherpa_num_threads,
                        provider=settings.asr_sherpa_provider,
                    )
                except AsrProviderUnavailable:
                    return FunAsrProvider(
                        "paraformer-zh-streaming",
                        "paraformer-zh-streaming",
                        streaming=True,
                        partial_window_seconds=settings.asr_partial_window_seconds,
                    )
            if name in {"sensevoice", "sensevoice-small"}:
                try:
                    return SherpaOnnxSenseVoiceProvider(
                        settings.asr_sensevoice_model_dir,
                        num_threads=settings.asr_sensevoice_num_threads,
                        provider=settings.asr_sensevoice_provider,
                        language=settings.asr_sensevoice_language,
                    )
                except AsrProviderUnavailable:
                    return FunAsrProvider(
                        "sensevoice",
                        "iic/SenseVoiceSmall",
                        streaming=False,
                        partial_window_seconds=settings.asr_partial_window_seconds,
                    )
            if name in {"whisper", "faster-whisper"}:
                return WhisperAsrProvider(
                    whisper,
                    partial_model=whisper_partial,
                    partial_window_seconds=settings.asr_partial_window_seconds,
                    initial_prompt=initial_prompt or settings.whisper_initial_prompt,
                    hotwords=settings.asr_hotwords,
                    no_speech_threshold=settings.asr_no_speech_threshold,
                    final_beam_size=settings.asr_final_beam_size,
                    final_vad_filter=settings.asr_final_vad_filter,
                    short_final_seconds=settings.asr_short_final_seconds,
                    short_final_beam_size=settings.asr_short_final_beam_size,
                )
        except AsrProviderUnavailable as exc:
            if logger is not None:
                logger(f"ASR provider {name} unavailable; trying the next provider: {exc}")
            continue
    if logger is not None:
        logger("All optional ASR providers were unavailable; using faster-whisper.")
    return WhisperAsrProvider(
        whisper,
        partial_model=whisper_partial,
        partial_window_seconds=settings.asr_partial_window_seconds,
        initial_prompt=initial_prompt or settings.whisper_initial_prompt,
        hotwords=settings.asr_hotwords,
        no_speech_threshold=settings.asr_no_speech_threshold,
        final_beam_size=settings.asr_final_beam_size,
        final_vad_filter=settings.asr_final_vad_filter,
        short_final_seconds=settings.asr_short_final_seconds,
        short_final_beam_size=settings.asr_short_final_beam_size,
    )
