from __future__ import annotations

import sys

from faster_whisper import WhisperModel

from jarvis.config import Settings
from jarvis.voice import synthesize_text

CHECK_TEXT = "你好，我是贾维斯。"


def main() -> int:
    settings = Settings()
    output_dir = settings.data_dir / "artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = output_dir / "voice_roundtrip.wav"
    voice_path = settings.data_dir / "piper" / f"{settings.tts_voice}.onnx"

    if not voice_path.exists():
        print(f"Piper voice not found: {voice_path}", file=sys.stderr)
        return 1
    synthesize_text(settings, CHECK_TEXT, audio_path)

    model = WhisperModel(
        settings.whisper_model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
    )
    segments, _ = model.transcribe(str(audio_path), language="zh", vad_filter=True)
    transcript = "".join(segment.text for segment in segments).strip()

    print(f"audio={audio_path}")
    print(f"expected={CHECK_TEXT}")
    print(f"transcript={transcript}")
    return 0 if transcript else 1


if __name__ == "__main__":
    raise SystemExit(main())
