from pathlib import Path

from jarvis.config import Settings


def test_empty_reasoning_effort_is_none(tmp_path: Path) -> None:
    settings = Settings(
        reasoning_effort="",
        data_dir=tmp_path / "data",
        workspace=tmp_path,
    )
    assert settings.reasoning_effort is None


def test_voice_output_limits_have_usable_defaults(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        workspace=tmp_path,
    )
    assert settings.voice_tts_chunk_chars == 100
    assert settings.voice_tts_max_concurrent == 4
    assert settings.voice_max_spoken_chars == 1_000
    assert settings.whisper_model == "small"
    assert settings.whisper_partial_model == "base"
    assert settings.asr_final_beam_size == 2
