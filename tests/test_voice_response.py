import asyncio
import threading
import time
from pathlib import Path

import pytest

from jarvis.runtime import CancellationToken
from jarvis.voice import prepare_text_for_speech, split_text_for_speech
from jarvis.voice_response import StreamingVoiceResponseController, VoiceResponseController


class FakeSynthesizer:
    def __init__(self, delays: dict[str, float] | None = None) -> None:
        self.delays = delays or {}
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def synthesize_wav_bytes(self, text: str) -> tuple[bytes, int]:
        with self.lock:
            self.calls.append(text)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.delays.get(text, 0.01))
        with self.lock:
            self.active -= 1
        return f"wav:{text}".encode(), 24_000


class FakeStreamingSynthesizer:
    def __init__(self, delays: dict[str, float] | None = None) -> None:
        self.delays = delays or {}
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.lock = asyncio.Lock()

    async def stream_audio(self, text: str):
        async with self.lock:
            self.calls.append(text)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delays.get(text, 0.01))
            yield f"audio:{text}".encode(), "wav", 24_000
        finally:
            async with self.lock:
                self.active -= 1


@pytest.mark.asyncio
async def test_voice_response_controller_orders_audio_chunks() -> None:
    synthesizer = FakeSynthesizer(
        {
            "第一句。": 0.04,
            "第二句。": 0.01,
            "第三句。": 0.01,
        }
    )
    events: list[dict[str, object]] = []

    async def emit(payload: dict[str, object]) -> None:
        events.append(payload)

    controller = VoiceResponseController(synthesizer, emit, max_concurrent_tts=2)
    await controller.start()
    await controller.feed("第一句。")
    await controller.feed("第二句。")
    await controller.feed("第三句。")
    await controller.finish()

    chunks = [event for event in events if event["type"] == "audio_chunk"]
    assert [chunk["index"] for chunk in chunks] == [0, 1, 2]
    assert [chunk["sentence"] for chunk in chunks] == ["第一句。", "第二句。", "第三句。"]
    assert [event["type"] for event in events[:2]] == [
        "synthesis_started",
        "audio_started",
    ]
    assert synthesizer.max_active >= 2


@pytest.mark.asyncio
async def test_voice_response_controller_flushes_unpunctuated_text() -> None:
    synthesizer = FakeSynthesizer()
    events: list[dict[str, object]] = []

    async def emit(payload: dict[str, object]) -> None:
        events.append(payload)

    controller = VoiceResponseController(synthesizer, emit)
    await controller.feed("这是一句没有标点的回答")
    await controller.finish()

    assert synthesizer.calls == ["这是一句没有标点的回答"]
    assert any(event["type"] == "audio_chunk" for event in events)


@pytest.mark.asyncio
async def test_streaming_voice_response_overlaps_llm_and_tts() -> None:
    synthesizer = FakeStreamingSynthesizer(
        {
            "第一句。": 0.06,
            "第二句。": 0.05,
            "第三句。": 0.04,
        }
    )
    events: list[dict[str, object]] = []

    async def emit(payload: dict[str, object]) -> None:
        events.append(payload)

    controller = StreamingVoiceResponseController(
        synthesizer,
        emit,
        max_concurrent_tts=3,
    )
    started = asyncio.get_running_loop().time()
    await controller.feed("第一句。")
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.04

    await controller.feed("第二句。")
    await controller.feed("第三句。")
    await controller.finish()

    chunks = [event for event in events if event["type"] == "audio_chunk"]
    assert [chunk["sentence"] for chunk in chunks] == ["第一句。", "第二句。", "第三句。"]
    assert [chunk["index"] for chunk in chunks] == [0, 1, 2]
    assert synthesizer.max_active >= 2


@pytest.mark.asyncio
async def test_voice_response_controller_cancels_worker_tasks() -> None:
    synthesizer = FakeSynthesizer({"慢句。": 0.2})
    events: list[dict[str, object]] = []
    cancellation = CancellationToken()

    async def emit(payload: dict[str, object]) -> None:
        events.append(payload)

    controller = VoiceResponseController(
        synthesizer,
        emit,
        cancellation=cancellation,
    )
    await controller.start()
    await controller.feed("慢句。")
    await asyncio.sleep(0)
    await controller.abort("stop")

    assert cancellation.cancelled
    assert not any(event["type"] == "audio_chunk" for event in events)


def test_split_text_for_speech_splits_clauses() -> None:
    assert split_text_for_speech("先检查内存，再看后台程序。") == [
        "先检查内存，",
        "再看后台程序。",
    ]
    assert split_text_for_speech("x" * 250, max_chars=100)[-1] == "x" * 50


def test_prepare_text_for_speech_removes_screen_markup() -> None:
    prepared = prepare_text_for_speech(
        "## 结果\n- 已完成 **检查**。\n```python\nprint('hidden')\n```\n"
        "详情见 https://example.com/report"
    )
    assert "hidden" not in prepared
    assert "**" not in prepared
    assert "代码部分请查看屏幕。" in prepared
    assert "https://" not in prepared
    assert "链接" in prepared


def test_voice_response_module_has_no_temp_path_dependency() -> None:
    module_path = Path(__file__).parents[1] / "src" / "jarvis" / "voice_response.py"
    source = module_path.read_text(encoding="utf-8")
    assert "tempfile" not in source
    assert "Path" not in source
