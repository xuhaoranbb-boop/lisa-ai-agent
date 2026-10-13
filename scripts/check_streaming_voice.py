from __future__ import annotations

import asyncio
import io
import json
import os
import time
import wave

import aiohttp
import numpy as np

from jarvis.config import Settings
from jarvis.voice import SpeechSynthesizer

BRIDGE_URL = os.environ.get("JARVIS_BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/")
ASR_SAMPLE_RATE = 16_000


def resample_pcm16(audio: bytes, source_rate: int) -> bytes:
    samples = np.frombuffer(audio, dtype="<i2").astype(np.float32)
    if source_rate == ASR_SAMPLE_RATE or len(samples) == 0:
        return samples.astype("<i2").tobytes()
    target_length = max(1, round(len(samples) * ASR_SAMPLE_RATE / source_rate))
    source_positions = np.linspace(0, len(samples) - 1, num=target_length)
    resampled = np.interp(
        source_positions,
        np.arange(len(samples)),
        samples,
    )
    return np.clip(resampled, -32768, 32767).astype("<i2").tobytes()


def synthesize_pcm16(synthesizer: SpeechSynthesizer, text: str) -> bytes:
    wav_bytes, sample_rate = synthesizer.synthesize_wav_bytes(text)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        audio = wav_file.readframes(wav_file.getnframes())
    return resample_pcm16(audio, sample_rate)


async def run_asr_sequence(
    session: aiohttp.ClientSession,
    synthesizer: SpeechSynthesizer,
    phrases: list[str],
) -> None:
    websocket_url = BRIDGE_URL.replace("http://", "ws://") + "/ws/asr"
    async with session.ws_connect(websocket_url, heartbeat=20) as websocket:
        for phrase in phrases:
            pcm = synthesize_pcm16(synthesizer, phrase)
            started_at = time.perf_counter()
            await websocket.send_json({"type": "start"})
            ready = await websocket.receive_json(timeout=5)
            if ready.get("type") != "ready":
                raise RuntimeError(f"Expected ASR ready, got {ready}")
            for offset in range(0, len(pcm), 3_200):
                await websocket.send_bytes(pcm[offset : offset + 3_200])
                await asyncio.sleep(0.02)
            for _ in range(5):
                await websocket.send_bytes(b"\x00" * 3_200)
                await asyncio.sleep(0.02)
            await websocket.send_json({"type": "stop"})
            final_text = ""
            while True:
                message = await websocket.receive_json(timeout=20)
                if message.get("type") == "final":
                    final_text = str(message.get("text") or "")
                    break
            elapsed_ms = round((time.perf_counter() - started_at) * 1_000, 1)
            print(
                json.dumps(
                    {
                        "type": "asr_final",
                        "expected": phrase,
                        "transcript": final_text,
                        "elapsed_ms": elapsed_ms,
                        "socket_reused": True,
                    },
                    ensure_ascii=False,
                )
            )


async def run_text_stream(session: aiohttp.ClientSession, text: str) -> None:
    started_at = time.perf_counter()
    first_text_at: float | None = None
    first_audio_at: float | None = None
    done_at: float | None = None
    audio_chunks = 0
    latency: dict[str, object] = {}
    request_id = f"streaming-check-{int(time.time() * 1000)}"
    async with session.post(
        f"{BRIDGE_URL}/api/respond/stream",
        json={"text": text, "request_id": request_id},
    ) as response:
        async for raw_line in response.content:
            if not raw_line.strip():
                continue
            event = json.loads(raw_line)
            event_type = str(event.get("type") or "")
            if event_type == "text_delta" and first_text_at is None:
                first_text_at = time.perf_counter()
            elif event_type == "audio_chunk":
                audio_chunks += 1
                if first_audio_at is None:
                    first_audio_at = time.perf_counter()
            elif event_type == "done":
                done_at = time.perf_counter()
                latency = dict(event.get("latency") or {})
            elif event_type == "error":
                raise RuntimeError(str(event.get("error") or "stream failed"))
    if first_text_at is None or first_audio_at is None or done_at is None:
        raise RuntimeError("Streaming response was incomplete.")
    print(
        json.dumps(
            {
                "type": "voice_stream",
                "first_text_ms": round((first_text_at - started_at) * 1_000, 1),
                "first_audio_ms": round((first_audio_at - started_at) * 1_000, 1),
                "done_ms": round((done_at - started_at) * 1_000, 1),
                "audio_before_done": first_audio_at < done_at,
                "audio_chunks": audio_chunks,
                "latency": latency,
            },
            ensure_ascii=False,
        )
    )


async def run_cancellation(session: aiohttp.ClientSession) -> None:
    request_id = f"cancel-check-{int(time.time() * 1000)}"
    started_at = time.perf_counter()
    cancel_sent_at: float | None = None
    cancelled_at: float | None = None
    async with session.post(
        f"{BRIDGE_URL}/api/respond/stream",
        json={
            "text": "请用至少五百字详细解释流式语音架构，不要调用工具。",
            "request_id": request_id,
        },
    ) as response:
        async for raw_line in response.content:
            if not raw_line.strip():
                continue
            event = json.loads(raw_line)
            event_type = str(event.get("type") or "")
            if event_type == "audio_chunk" and cancel_sent_at is None:
                cancel_sent_at = time.perf_counter()
                await session.post(
                    f"{BRIDGE_URL}/api/cancel",
                    json={"request_id": request_id, "reason": "barge-in check"},
                )
            elif event_type == "cancelled":
                cancelled_at = time.perf_counter()
                break
            elif event_type == "done":
                raise RuntimeError("Cancellation was ignored; stream completed.")
    if cancel_sent_at is None or cancelled_at is None:
        raise RuntimeError("Cancellation check did not observe a chunk and cancellation.")
    print(
        json.dumps(
            {
                "type": "cancellation",
                "cancel_after_first_audio_ms": round(
                    (cancel_sent_at - started_at) * 1_000,
                    1,
                ),
                "cancelled_after_request_ms": round(
                    (cancelled_at - cancel_sent_at) * 1_000,
                    1,
                ),
            },
            ensure_ascii=False,
        )
    )


async def run_tool_stream(session: aiohttp.ClientSession) -> None:
    request_id = f"tool-stream-check-{int(time.time() * 1000)}"
    reply = ""
    latency: dict[str, object] = {}
    async with session.post(
        f"{BRIDGE_URL}/api/respond/stream",
        json={
            "text": "现在上海几点？请调用时间工具后简短回答。",
            "request_id": request_id,
        },
    ) as response:
        async for raw_line in response.content:
            if not raw_line.strip():
                continue
            event = json.loads(raw_line)
            if event.get("type") == "text_delta":
                reply += str(event.get("delta") or "")
            elif event.get("type") == "done":
                reply = str(event.get("reply") or reply)
                latency = dict(event.get("latency") or {})
            elif event.get("type") == "error":
                raise RuntimeError(str(event.get("error") or "stream failed"))
    print(
        json.dumps(
            {
                "type": "tool_stream",
                "request_id": request_id,
                "reply": reply,
                "latency": latency,
            },
            ensure_ascii=False,
        )
    )


async def main() -> None:
    settings = Settings()
    synthesizer = SpeechSynthesizer(settings)
    phrases = ["你好，贾维斯。", "请记住我喜欢简洁的回答。"]
    async with aiohttp.ClientSession() as session:
        await run_asr_sequence(session, synthesizer, phrases)
        await run_text_stream(
            session,
            "请用两句话简短说明流式语音的好处，不要调用工具。",
        )
        await run_cancellation(session)
        await run_tool_stream(session)


if __name__ == "__main__":
    asyncio.run(main())
