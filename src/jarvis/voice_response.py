from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from jarvis.runtime import CancellationToken, OperationCancelled
from jarvis.voice import SpeechSynthesizer, prepare_text_for_speech, split_text_for_speech


@dataclass(frozen=True)
class VoiceAudioChunk:
    index: int
    sentence: str
    audio: bytes
    sample_rate: int


StreamEventEmitter = Callable[[dict[str, Any]], Awaitable[None]]


def _pop_ready_speech(
    text: str,
    *,
    max_chars: int = 120,
    force: bool = False,
) -> tuple[list[str], str]:
    """Return speakable chunks without waiting for an entire LLM answer."""
    if not text:
        return [], ""
    if force:
        return split_text_for_speech(text, max_chars=max_chars), ""

    boundaries = ("。", "！", "？", "!", "?", "；", ";", "，", ",", "\n")
    cut = -1
    for index, character in enumerate(text):
        if character in boundaries:
            cut = index + 1
    if cut <= 0:
        if len(text) < max_chars:
            return [], text
        cut = max_chars
    head = text[:cut]
    return split_text_for_speech(head, max_chars=max_chars), text[cut:]


class VoiceResponseController:
    """Turn streamed text into ordered, concurrently synthesized audio chunks."""

    def __init__(
        self,
        synthesizer: SpeechSynthesizer,
        emit: StreamEventEmitter,
        *,
        cancellation: CancellationToken | None = None,
        request_id: str | None = None,
        max_concurrent_tts: int = 2,
        max_spoken_chars: int = 900,
        max_chars_per_chunk: int = 80,
    ) -> None:
        self.synthesizer = synthesizer
        self.emit = emit
        self.cancellation = cancellation or CancellationToken()
        self.request_id = request_id
        self.max_concurrent_tts = max(1, max_concurrent_tts)
        self.max_spoken_chars = max(1, max_spoken_chars)
        self.max_chars_per_chunk = max(24, max_chars_per_chunk)
        self._sentence_queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue(
            maxsize=self.max_concurrent_tts * 4
        )
        self._audio_queue: asyncio.PriorityQueue[
            tuple[int, VoiceAudioChunk | Exception | None]
        ] = asyncio.PriorityQueue()
        self._buffer = ""
        self._next_index = 0
        self._accepted_chars = 0
        self._started = False
        self._finished = False
        self._aborted = False
        self._workers: list[asyncio.Task[None]] = []
        self._writer: asyncio.Task[None] | None = None
        self._first_synthesis_started = False
        self._first_audio_emitted = False
        self.audio_chunks = 0

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._writer = asyncio.create_task(self._drain_audio())
        self._workers = [
            asyncio.create_task(self._synthesis_worker())
            for _ in range(self.max_concurrent_tts)
        ]

    async def feed(self, delta: str) -> None:
        if not delta or self._finished or self._aborted:
            return
        if not self._started:
            await self.start()
        self.cancellation.raise_if_cancelled()
        self._buffer += delta
        chunks, self._buffer = _pop_ready_speech(
            self._buffer,
            max_chars=self.max_chars_per_chunk,
        )
        for chunk in chunks:
            await self._enqueue(chunk)

    async def finish(self) -> None:
        if self._aborted:
            return
        if self._finished:
            if self._writer is not None:
                await self._writer
            return
        if not self._started:
            await self.start()
        self._finished = True
        self.cancellation.raise_if_cancelled()
        chunks, self._buffer = _pop_ready_speech(
            self._buffer,
            max_chars=self.max_chars_per_chunk,
            force=True,
        )
        for chunk in chunks:
            await self._enqueue(chunk)
        for _ in self._workers:
            await self._sentence_queue.put(None)
        await asyncio.gather(*self._workers)
        await self._audio_queue.put((sys.maxsize, None))
        if self._writer is not None:
            await self._writer

    async def abort(self, reason: str = "Voice response cancelled.") -> None:
        if self._aborted:
            return
        self._aborted = True
        self.cancellation.cancel(reason)
        tasks = [*self._workers]
        if self._writer is not None:
            tasks.append(self._writer)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def finished(self) -> bool:
        return self._finished

    async def _enqueue(self, sentence: str) -> None:
        sentence = prepare_text_for_speech(sentence)
        if not sentence:
            return
        remaining = self.max_spoken_chars - self._accepted_chars
        if remaining <= 0:
            return
        if len(sentence) > remaining:
            sentence = sentence[: max(1, remaining - 1)].rstrip() + "…"
        self._accepted_chars += len(sentence)
        if not self._first_synthesis_started:
            self._first_synthesis_started = True
            await self.emit(
                {
                    "type": "synthesis_started",
                    "request_id": self.request_id,
                    "index": self._next_index,
                }
            )
        await self._sentence_queue.put((self._next_index, sentence))
        self._next_index += 1

    async def _synthesis_worker(self) -> None:
        while True:
            item = await self._sentence_queue.get()
            if item is None:
                return
            index, sentence = item
            try:
                self.cancellation.raise_if_cancelled()
                audio, sample_rate = await asyncio.to_thread(
                    self.synthesizer.synthesize_wav_bytes,
                    sentence,
                )
                self.cancellation.raise_if_cancelled()
                await self._audio_queue.put(
                    (
                        index,
                        VoiceAudioChunk(
                            index=index,
                            sentence=sentence,
                            audio=audio,
                            sample_rate=sample_rate,
                        ),
                    )
                )
            except OperationCancelled:
                return
            except Exception as exc:
                await self._audio_queue.put((index, exc))

    async def _drain_audio(self) -> None:
        expected = 0
        pending: dict[int, VoiceAudioChunk | Exception | None] = {}
        while True:
            index, result = await self._audio_queue.get()
            if index == sys.maxsize:
                return
            pending[index] = result
            while expected in pending:
                ready = pending.pop(expected)
                if isinstance(ready, Exception):
                    await self.emit(
                        {
                            "type": "audio_error",
                            "request_id": self.request_id,
                            "index": expected,
                            "error": f"{type(ready).__name__}: {ready}",
                        }
                    )
                elif isinstance(ready, VoiceAudioChunk):
                    if not self._first_audio_emitted:
                        self._first_audio_emitted = True
                        await self.emit(
                            {
                                "type": "audio_started",
                                "request_id": self.request_id,
                                "index": ready.index,
                            }
                        )
                    await self.emit(
                        {
                            "type": "audio_chunk",
                            "request_id": self.request_id,
                            "index": ready.index,
                            "sentence": ready.sentence,
                            "audio": ready.audio,
                            "sample_rate": ready.sample_rate,
                        }
                    )
                    self.audio_chunks += 1
                expected += 1


class StreamingVoiceResponseController:
    """Synthesize sentences concurrently while preserving playback order."""

    def __init__(
        self,
        synthesizer: SpeechSynthesizer,
        emit: StreamEventEmitter,
        *,
        cancellation: CancellationToken | None = None,
        request_id: str | None = None,
        max_concurrent_tts: int = 2,
        max_spoken_chars: int = 900,
        max_chars_per_chunk: int = 80,
    ) -> None:
        self.synthesizer = synthesizer
        self.emit = emit
        self.cancellation = cancellation or CancellationToken()
        self.request_id = request_id
        self.max_concurrent_tts = max(1, max_concurrent_tts)
        self.max_spoken_chars = max(1, max_spoken_chars)
        self.max_chars_per_chunk = max(24, max_chars_per_chunk)
        self._sentence_queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue(
            maxsize=self.max_concurrent_tts * 4
        )
        self._audio_queue: asyncio.PriorityQueue[
            tuple[int, int, dict[str, Any] | Exception | None]
        ] = asyncio.PriorityQueue()
        self._buffer = ""
        self._next_index = 0
        self._accepted_chars = 0
        self._started = False
        self._finished = False
        self._aborted = False
        self._workers: list[asyncio.Task[None]] = []
        self._writer: asyncio.Task[None] | None = None
        self._first_audio_emitted = False
        self.audio_chunks = 0

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._writer = asyncio.create_task(self._drain_audio())
        self._workers = [
            asyncio.create_task(self._synthesis_worker())
            for _ in range(self.max_concurrent_tts)
        ]

    async def feed(self, delta: str) -> None:
        if not delta or self._finished or self._aborted:
            return
        if not self._started:
            await self.start()
        self.cancellation.raise_if_cancelled()
        self._buffer += delta
        chunks, self._buffer = _pop_ready_speech(
            self._buffer,
            max_chars=self.max_chars_per_chunk,
        )
        for chunk in chunks:
            await self._enqueue(chunk)

    async def finish(self) -> None:
        if self._aborted or self._finished:
            return
        if not self._started:
            await self.start()
        self._finished = True
        self.cancellation.raise_if_cancelled()
        chunks, self._buffer = _pop_ready_speech(
            self._buffer,
            max_chars=self.max_chars_per_chunk,
            force=True,
        )
        for chunk in chunks:
            await self._enqueue(chunk)
        for _ in self._workers:
            await self._sentence_queue.put(None)
        await asyncio.gather(*self._workers)
        await self._audio_queue.put((sys.maxsize, 0, None))
        if self._writer is not None:
            await self._writer

    async def abort(self, reason: str = "Voice response cancelled.") -> None:
        if self._aborted:
            return
        self._aborted = True
        self.cancellation.cancel(reason)
        tasks = [*self._workers]
        if self._writer is not None:
            tasks.append(self._writer)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def finished(self) -> bool:
        return self._finished

    async def _enqueue(self, sentence: str) -> None:
        sentence = prepare_text_for_speech(sentence)
        if not sentence:
            return
        remaining = self.max_spoken_chars - self._accepted_chars
        if remaining <= 0:
            return
        if len(sentence) > remaining:
            sentence = sentence[: max(1, remaining - 1)].rstrip() + "…"
        self._accepted_chars += len(sentence)
        index = self._next_index
        await self._audio_queue.put(
            (
                index,
                0,
                {
                    "kind": "synthesis_started",
                    "sentence": sentence,
                },
            )
        )
        await self._sentence_queue.put((index, sentence))
        self._next_index += 1

    async def _synthesis_worker(self) -> None:
        while True:
            item = await self._sentence_queue.get()
            if item is None:
                return
            index, sentence = item
            try:
                self.cancellation.raise_if_cancelled()
                chunk_index = 1
                async for audio, audio_format, sample_rate in self.synthesizer.stream_audio(
                    sentence
                ):
                    self.cancellation.raise_if_cancelled()
                    await self._audio_queue.put(
                        (
                            index,
                            chunk_index,
                            {
                                "kind": "audio_chunk",
                                "sentence": sentence,
                                "audio": audio,
                                "audio_format": audio_format,
                                "audio_sample_rate": sample_rate,
                            },
                        )
                    )
                    chunk_index += 1
                await self._audio_queue.put(
                    (index, chunk_index, {"kind": "sentence_complete"})
                )
            except OperationCancelled:
                return
            except Exception as exc:
                await self._audio_queue.put((index, 1, exc))
                await self._audio_queue.put(
                    (index, 2, {"kind": "sentence_complete"})
                )

    async def _drain_audio(self) -> None:
        expected_index = 0
        expected_chunk = 0
        pending: dict[tuple[int, int], dict[str, Any] | Exception | None] = {}

        async def emit_next() -> bool:
            nonlocal expected_index, expected_chunk
            key = (expected_index, expected_chunk)
            if key not in pending:
                return False
            payload = pending.pop(key)
            if isinstance(payload, Exception):
                await self.emit(
                    {
                        "type": "audio_error",
                        "request_id": self.request_id,
                        "index": expected_index,
                        "error": f"{type(payload).__name__}: {payload}",
                    }
                )
                expected_chunk += 1
                return True
            if not payload:
                expected_chunk += 1
                return True
            event_type = str(payload.get("kind") or "")
            if event_type == "synthesis_started":
                await self.emit(
                    {
                        "type": "synthesis_started",
                        "request_id": self.request_id,
                        "index": expected_index,
                        "sentence": payload.get("sentence", ""),
                    }
                )
                expected_chunk += 1
                return True
            if event_type == "sentence_complete":
                expected_index += 1
                expected_chunk = 0
                return True
            if event_type == "audio_chunk":
                if not self._first_audio_emitted:
                    self._first_audio_emitted = True
                    await self.emit(
                        {
                            "type": "audio_started",
                            "request_id": self.request_id,
                            "index": expected_index,
                        }
                    )
                self.audio_chunks += 1
                await self.emit(
                    {
                        "type": "audio_chunk",
                        "request_id": self.request_id,
                        "index": self.audio_chunks - 1,
                        "sentence": payload.get("sentence", ""),
                        "audio": payload.get("audio", b""),
                        "audio_format": payload.get("audio_format", "wav"),
                        "audio_sample_rate": payload.get("audio_sample_rate", 24_000),
                    }
                )
                expected_chunk += 1
                return True
            expected_chunk += 1
            return True

        while True:
            index, chunk_index, payload = await self._audio_queue.get()
            if index == sys.maxsize:
                while await emit_next():
                    pass
                return
            pending[(index, chunk_index)] = payload
            while await emit_next():
                pass
