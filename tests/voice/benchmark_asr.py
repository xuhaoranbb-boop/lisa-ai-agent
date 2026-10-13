from __future__ import annotations

import asyncio
import difflib
import json
import re
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path

from faster_whisper import WhisperModel

from jarvis.asr_provider import create_asr_provider
from jarvis.config import Settings

PHRASES = [
    "你好贾维斯",
    "打开浏览器",
    "打开微信",
    "帮我截图",
    "停止",
    "暂停",
    "关闭窗口",
    "继续",
    "取消刚才的任务",
    "搜索一下今天的天气",
]


def _character_error_rate(expected: str, actual: str) -> float:
    def normalizer(value: str) -> str:
        return re.sub(
            r"[\s，。！？!?；;：:、,.]+",
            "",
            value.lower().replace("jarvis", "贾维斯"),
        )

    expected_chars = list(normalizer(expected))
    actual_chars = list(normalizer(actual))
    if not expected_chars:
        return 0.0 if not actual_chars else 1.0
    ratio = difflib.SequenceMatcher(None, expected_chars, actual_chars).ratio()
    return max(0.0, 1.0 - ratio)


def _generate_sapi_wavs(output_dir: Path, phrases: list[str]) -> list[Path]:
    payload_path = output_dir / "phrases.json"
    payload_path.write_text(json.dumps(phrases, ensure_ascii=False), encoding="utf-8")
    pwsh = shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"
    script = f"""
Add-Type -AssemblyName System.Speech
$phrases = Get-Content -Raw -Encoding UTF8 -LiteralPath '{payload_path}' | ConvertFrom-Json
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voice = $synth.GetInstalledVoices() |
  Where-Object {{ $_.VoiceInfo.Culture.Name -eq 'zh-CN' }} |
  Select-Object -First 1
if ($voice) {{ $synth.SelectVoice($voice.VoiceInfo.Name) }}
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
  16000,
  [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
  [System.Speech.AudioFormat.AudioChannel]::Mono
)
$index = 0
foreach ($phrase in $phrases) {{
  $path = Join-Path '{output_dir}' ("sample-" + $index + ".wav")
  $synth.SetOutputToWaveFile($path, $format)
  $synth.Speak([string]$phrase)
  $synth.SetOutputToNull()
  $index++
}}
"""
    subprocess.run(
        [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return [output_dir / f"sample-{index}.wav" for index in range(len(phrases))]


def _read_pcm16(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2 or handle.getnchannels() != 1:
            raise ValueError(f"Expected mono PCM16 WAV: {path}")
        return handle.readframes(handle.getnframes())


async def _run() -> None:
    settings = Settings(_env_file=None)
    final_model = WhisperModel(
        settings.whisper_model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
    )
    partial_model = WhisperModel(
        settings.whisper_partial_model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
    )

    with tempfile.TemporaryDirectory(
        prefix="jarvis-asr-bench-",
        dir=r"C:\Users\Public",
    ) as temp:
        output_dir = Path(temp)
        wavs = _generate_sapi_wavs(output_dir, PHRASES)
        print("ASR benchmark uses offline Windows SAPI speech, not the live microphone.")
        print(f"provider_order={settings.asr_provider_order}")
        print("")
        print(
            f"{'phrase':<20} {'heard':<28} {'CER':>6} "
            f"{'partial_ms':>10} {'final_ms':>9}"
        )
        total_cer = 0.0
        total_ms = 0.0
        total_partial_ms = 0.0
        for expected, wav in zip(PHRASES, wavs, strict=True):
            provider = create_asr_provider(
                settings,
                final_model,
                partial_model,
                initial_prompt=settings.whisper_initial_prompt,
            )
            await provider.start_session(session_id="benchmark", request_id=expected)
            pcm = _read_pcm16(wav)
            first_window = min(len(pcm), 16_000 * 2)
            for offset in range(0, first_window, 640):
                provider.push_audio(pcm[offset : offset + 640])
            partial_started = time.perf_counter()
            await provider.partial()
            partial_ms = (time.perf_counter() - partial_started) * 1_000
            for offset in range(first_window, len(pcm), 640):
                provider.push_audio(pcm[offset : offset + 640])
            started = time.perf_counter()
            event = await provider.flush()
            elapsed_ms = (time.perf_counter() - started) * 1_000
            await provider.close()
            cer = _character_error_rate(expected, event.final_text)
            total_cer += cer
            total_ms += elapsed_ms
            total_partial_ms += partial_ms
            print(
                f"{expected:<20} {event.final_text:<28} "
                f"{cer:>6.2%} {partial_ms:>10.1f} {elapsed_ms:>9.1f}"
            )
        count = max(1, len(PHRASES))
        print("")
        print(f"average CER={total_cer / count:.2%}")
        print(f"average partial latency={total_partial_ms / count:.1f} ms")
        print(f"average final latency={total_ms / count:.1f} ms")


if __name__ == "__main__":
    asyncio.run(_run())
