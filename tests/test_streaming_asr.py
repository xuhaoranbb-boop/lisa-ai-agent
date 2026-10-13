from dataclasses import dataclass

import pytest

from jarvis.streaming_asr import (
    StreamingWhisperASR,
    build_dynamic_initial_prompt,
    normalize_transcript,
)


@dataclass
class FakeSegment:
    text: str


class FakeWhisper:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def transcribe(self, _audio, **kwargs):  # type: ignore[no-untyped-def]
        self.kwargs = kwargs
        return [FakeSegment("你好，贾维斯。")], None


@pytest.mark.asyncio
async def test_streaming_asr_uses_jarvis_vocabulary_prompt() -> None:
    model = FakeWhisper()
    transcriber = StreamingWhisperASR(model)  # type: ignore[arg-type]
    transcriber.feed(b"\x00\x00" * 48_000)

    assert await transcriber.final() == "你好，贾维斯。"
    assert model.kwargs["language"] == "zh"
    assert "DeepSeek" in str(model.kwargs["initial_prompt"])
    assert model.kwargs["no_speech_threshold"] == 0.6


def test_transcript_corrects_only_known_jarvis_confusions() -> None:
    assert normalize_transcript("你好，古维斯。") == "你好，贾维斯。"
    assert normalize_transcript("这是一个普通句子。") == "这是一个普通句子。"


def test_transcript_normalizes_chinese_spacing_and_artifacts() -> None:
    assert normalize_transcript("请 帮 我 打开 VS Code 。") == "请帮我打开 VS Code。"
    assert normalize_transcript("谢谢观看 你好 贾维斯") == "你好贾维斯"
    assert normalize_transcript("v s code") == "VS Code"
    assert normalize_transcript("deep seek") == "DeepSeek"


def test_dynamic_asr_prompt_biases_user_vocabulary() -> None:
    prompt = build_dynamic_initial_prompt(
        "以下是中文语音对话。",
        [
            "用户常用项目 GCAI JARVIS，编辑器是 VS Code。",
            "常用工具 DeepSeek 和 Ollama，重复项 VS Code。",
        ],
    )

    assert "GCAI" in prompt
    assert "JARVIS" in prompt
    assert "VS Code" in prompt or "Code" in prompt
    assert "DeepSeek" in prompt
    assert "Ollama" in prompt


def test_dynamic_asr_prompt_drops_credential_like_memory() -> None:
    prompt = build_dynamic_initial_prompt(
        "基础提示。",
        ["api_key=sk-deadbeefdeadbeefdeadbeefdeadbeef", "项目代号朱雀"],
    )

    assert "sk-" not in prompt
    assert "api_key" not in prompt
    assert "朱雀" in prompt


@pytest.mark.asyncio
async def test_partial_uses_fast_model_and_hotwords() -> None:
    final_model = FakeWhisper()
    partial_model = FakeWhisper()
    transcriber = StreamingWhisperASR(
        final_model,  # type: ignore[arg-type]
        partial_model=partial_model,  # type: ignore[arg-type]
        hotwords="贾维斯,DeepSeek",
    )
    transcriber.feed(b"\x00\x00" * 48_000)

    await transcriber.partial()
    await transcriber.final()

    assert partial_model.kwargs["hotwords"] == "贾维斯,DeepSeek"
    assert final_model.kwargs["hotwords"] == "贾维斯,DeepSeek"
