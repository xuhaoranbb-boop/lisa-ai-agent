from pathlib import Path

from jarvis.config import Settings
from jarvis.persona import (
    DynamicPersona,
    PersonaManager,
    PersonaMode,
    PersonaProfile,
    PersonaStore,
)


def test_persona_modes_are_inferred_and_explicit_mode_is_persisted(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path)
    manager = PersonaManager(settings)

    assert manager.mode_for_message("请简短回答") == PersonaMode.BRIEF
    assert manager.mode_for_message("我很累，陪我聊会儿") == PersonaMode.COMPANION
    assert manager.mode_for_message("检查项目并运行测试") == PersonaMode.FOCUSED
    assert manager.mode_for_message("系统失败了，紧急处理") == PersonaMode.ALERT

    manager.profile_for_message("切换到专注模式")
    assert manager.profile.default_mode == PersonaMode.FOCUSED

    reloaded = PersonaManager(settings)
    assert reloaded.profile.default_mode == PersonaMode.FOCUSED


def test_persona_preferences_persist_and_change_voice_budget(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path)
    manager = PersonaManager(settings)

    profile = manager.profile_for_message("以后回答简短一点")
    assert profile.verbosity == 2
    assert manager.spoken_char_budget("以后回答简短一点", 900) == 260

    reloaded = PersonaManager(settings)
    assert reloaded.profile.verbosity == 2


def test_persona_store_round_trip(tmp_path: Path) -> None:
    profile = PersonaProfile(name="GCAI", version="2.1", warmth=5)
    store = PersonaStore(tmp_path / "persona.json")
    store.save(profile)

    loaded = store.load(PersonaProfile())
    assert loaded.name == "GCAI"
    assert loaded.version == "2.1"
    assert loaded.warmth == 5


def test_bundled_educated_butler_persona_imports(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path)
    manager = PersonaManager(settings)
    source = Path(__file__).parents[1] / "personas" / "educated_butler.json"

    imported = manager.import_profile(source)

    assert imported.version == "2.0"
    assert imported.archetype == "highly educated career private butler"
    assert imported.default_mode == PersonaMode.BUTLER
    assert "Project Gutenberg #10136" in imported.source_basis[0]
    assert PersonaManager(settings).profile.version == "2.0"


def test_dynamic_persona_uses_scene_overrides(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path)
    persona = DynamicPersona(settings)

    assert "Current mode: alert." in persona.prompt_for_context(
        "普通问题",
        urgent=True,
    )
    assert "Current mode: focused." in persona.prompt_for_context(
        "普通问题",
        task_active=True,
    )


def test_persona_prompt_block_accepts_string_mode_and_coerces_default(
    tmp_path: Path,
) -> None:
    profile = PersonaProfile().model_copy(update={"default_mode": "butler"})

    assert "Current mode: butler." in profile.prompt_block("butler")

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path)
    manager = PersonaManager(settings)
    manager.profile = profile

    assert manager.mode_for_message("hello") == PersonaMode.BUTLER
