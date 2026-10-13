from dataclasses import dataclass

import pytest

from jarvis.asr_provider import WhisperAsrProvider, create_asr_provider
from jarvis.config import Settings


@dataclass
class FakeSegment:
    text: str


class FakeWhisper:
    def transcribe(self, _audio, **_kwargs):  # type: ignore[no-untyped-def]
        return [FakeSegment("你好，贾维斯。")], None


@pytest.mark.asyncio
async def test_whisper_provider_contract() -> None:
    provider = WhisperAsrProvider(FakeWhisper())  # type: ignore[arg-type]

    started = await provider.start_session(session_id="s1", request_id="r1")
    provider.push_audio(b"\x00\x00" * 16_000)
    partial = await provider.partial()
    final = await provider.flush()
    await provider.cancel()
    await provider.close()

    assert started.provider == "whisper"
    assert partial.partial_text == "你好，贾维斯。"
    assert final.final_text == "你好，贾维斯。"
    assert final.is_final is True
    assert final.request_id == "r1"
    assert final.end_ms >= 0


def test_factory_falls_back_to_whisper_when_optional_providers_are_missing() -> None:
    settings = Settings(
        _env_file=None,
        asr_provider_order="qwen3-asr,paraformer-zh-streaming,sensevoice,whisper",
        asr_sherpa_model_dir="C:/definitely-missing-jarvis-asr-model",
        asr_sensevoice_model_dir="C:/definitely-missing-jarvis-sensevoice-model",
    )

    provider = create_asr_provider(settings, FakeWhisper())  # type: ignore[arg-type]

    assert provider.name == "whisper"
