from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from jarvis.config import Settings


class PersonaMode(StrEnum):
    BUTLER = "butler"
    FOCUSED = "focused"
    BRIEF = "brief"
    COMPANION = "companion"
    ALERT = "alert"


class PersonaProfile(BaseModel):
    name: str = "Jarvis"
    version: str = "1.0"
    archetype: str = "calm senior personal butler"
    background: str = ""
    education: list[str] = Field(default_factory=list)
    values: list[str] = Field(default_factory=list)
    speech_style: list[str] = Field(default_factory=list)
    service_principles: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    source_basis: list[str] = Field(default_factory=list)
    default_mode: PersonaMode = PersonaMode.BUTLER
    address_user_as: str = ""
    language: str = "auto"
    formality: int = Field(default=4, ge=1, le=5)
    warmth: int = Field(default=3, ge=1, le=5)
    directness: int = Field(default=4, ge=1, le=5)
    humor: int = Field(default=1, ge=1, le=5)
    proactivity: int = Field(default=4, ge=1, le=5)
    verbosity: int = Field(default=2, ge=1, le=5)
    forbidden_phrases: list[str] = Field(
        default_factory=lambda: [
            "好的，我现在将为您执行",
            "作为一个AI语言模型",
            "请告诉我你的CPU使用率",
        ]
    )

    def prompt_block(self, mode: PersonaMode | str) -> str:
        if not isinstance(mode, PersonaMode):
            mode = PersonaMode(mode)
        address = (
            f'Address the user as "{self.address_user_as}".'
            if self.address_user_as
            else "Use the user's name sparingly and naturally."
        )
        mode_rule = _MODE_RULES[mode]
        forbidden = "; ".join(self.forbidden_phrases[:6])
        background = self.background or "Experienced in discreet personal assistance."
        education = ", ".join(self.education[:5]) or "Practical education and continuous learning."
        values = ", ".join(self.values[:6]) or "competence, discretion, reliability"
        speech_style = (
            ", ".join(self.speech_style[:6])
            or "concise, composed, precise, natural"
        )
        principles = (
            "; ".join(self.service_principles[:6])
            or "anticipate needs, verify before acting, report outcomes plainly"
        )
        boundaries_text = (
            "; ".join(self.boundaries[:5])
            or (
                "never overstate certainty, never bypass permissions, "
                "never impersonate a real person"
            )
        )
        return f"""
Persona profile:
- Identity: {self.name}, version {self.version}.
- Archetype: {self.archetype}.
- Background: {background}
- Education and expertise: {education}
- Values: {values}
- Speech style: {speech_style}
- Service principles: {principles}
- Boundaries: {boundaries_text}
- Current mode: {mode.value}.
- {address}
- Formality {self.formality}/5, warmth {self.warmth}/5, directness {self.directness}/5,
  humor {self.humor}/5, proactivity {self.proactivity}/5, verbosity {self.verbosity}/5.
- {mode_rule}
- Never use these canned or evasive phrases: {forbidden}.
- Keep one stable identity across text and voice. Adapt tone to the situation, not the facts.
""".strip()


_MODE_RULES = {
    PersonaMode.BUTLER: (
        "Act like a calm senior butler: anticipate the next useful step, stay composed, "
        "and report outcomes without theatrical role-play."
    ),
    PersonaMode.FOCUSED: (
        "Be compact and execution-oriented. Lead with status, blockers, and the next action."
    ),
    PersonaMode.BRIEF: (
        "Answer in one to three short sentences. Omit background unless it changes the decision."
    ),
    PersonaMode.COMPANION: (
        "Be warmer and more attentive. Acknowledge the user's state before offering practical help."
    ),
    PersonaMode.ALERT: (
        "Be immediate and unambiguous. State the risk first, then the safest next action."
    ),
}


class PersonaStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, default: PersonaProfile) -> PersonaProfile:
        if not self.path.exists():
            return default
        try:
            return PersonaProfile.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return default

    def save(self, profile: PersonaProfile) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                profile.model_dump_json(indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)


class PersonaManager:
    """Own persona identity, dynamic mode, and durable user style preferences."""

    def __init__(self, settings: Settings) -> None:
        default = PersonaProfile(
            name=settings.assistant_name,
            default_mode=PersonaMode(settings.persona_mode),
            address_user_as=settings.user_name,
        )
        self.store = PersonaStore(settings.data_dir / "persona.json")
        self.profile = self.store.load(default)

    def profile_for_message(self, text: str) -> PersonaProfile:
        updated = self.apply_explicit_preferences(text)
        return updated

    def import_profile(self, path: Path) -> PersonaProfile:
        profile = PersonaProfile.model_validate_json(
            path.expanduser().resolve().read_text(encoding="utf-8")
        )
        self.profile = profile
        self.store.save(profile)
        return profile

    def prompt_for(self, text: str = "") -> str:
        profile = self.profile_for_message(text)
        return profile.prompt_block(self.mode_for_message(text))

    def spoken_char_budget(self, text: str, default: int) -> int:
        mode = self.mode_for_message(text)
        if mode == PersonaMode.BRIEF:
            return min(default, 260)
        if mode == PersonaMode.ALERT:
            return min(default, 420)
        if mode == PersonaMode.FOCUSED:
            return min(default, 700)
        return default

    def mode_for_message(self, text: str) -> PersonaMode:
        lowered = text.lower()
        explicit = _explicit_mode(lowered)
        if explicit is not None:
            return explicit
        if any(
            term in lowered
            for term in (
                "紧急",
                "危险",
                "出错了",
                "失败了",
                "报警",
                "urgent",
                "emergency",
                "critical",
            )
        ):
            return PersonaMode.ALERT
        if any(
            term in lowered
            for term in (
                "累",
                "烦",
                "难受",
                "心情",
                "陪我",
                "安慰",
                "压力",
                "lonely",
                "stressed",
                "upset",
            )
        ):
            return PersonaMode.COMPANION
        if any(
            term in lowered
            for term in (
                "一句话",
                "简短",
                "简单说",
                "不用解释",
                "短一点",
                "brief",
                "one sentence",
            )
        ):
            return PersonaMode.BRIEF
        if any(
            term in lowered
            for term in (
                "检查项目",
                "运行测试",
                "修复",
                "重构",
                "排查",
                "执行",
                "debug",
                "refactor",
                "run tests",
                "inspect the project",
            )
        ):
            return PersonaMode.FOCUSED
        return PersonaMode(self.profile.default_mode)

    def apply_explicit_preferences(self, text: str) -> PersonaProfile:
        lowered = text.lower()
        updates: dict[str, object] = {}
        if any(term in lowered for term in ("以后回答简短", "回答简洁", "说话简短", "简洁一点")):
            updates["verbosity"] = 2
        if any(term in lowered for term in ("以后详细", "详细一点", "解释详细", "多说一点")):
            updates["verbosity"] = 4
        if any(term in lowered for term in ("正式一点", "专业正式", "更正式")):
            updates["formality"] = 5
        if any(term in lowered for term in ("轻松一点", "幽默一点", "有趣一点")):
            updates["humor"] = min(5, self.profile.humor + 1)
        if any(term in lowered for term in ("主动一点", "不用每次都问", "自己判断")):
            updates["proactivity"] = 5
        explicit_mode = _explicit_mode(lowered)
        if explicit_mode is not None:
            updates["default_mode"] = explicit_mode
        if not updates:
            return self.profile
        self.profile = self.profile.model_copy(update=updates)
        self.store.save(self.profile)
        return self.profile


def _explicit_mode(text: str) -> PersonaMode | None:
    mappings = {
        PersonaMode.BUTLER: ("管家模式", "butler mode"),
        PersonaMode.FOCUSED: ("专注模式", "工作模式", "focus mode"),
        PersonaMode.BRIEF: ("简短模式", "简洁模式", "brief mode"),
        PersonaMode.COMPANION: ("陪伴模式", "聊天模式", "companion mode"),
        PersonaMode.ALERT: ("警报模式", "紧急模式", "alert mode"),
    }
    for mode, phrases in mappings.items():
        if any(phrase in text for phrase in phrases):
            return mode
    return None


class DynamicPersona(PersonaManager):
    """Persona manager with situation-aware tone overrides."""

    def prompt_for_context(
        self,
        text: str = "",
        *,
        task_active: bool = False,
        error: bool = False,
        urgent: bool = False,
    ) -> str:
        self.profile_for_message(text)
        if urgent:
            mode = PersonaMode.ALERT
        elif error:
            mode = PersonaMode.FOCUSED
        elif task_active:
            mode = PersonaMode.FOCUSED
        else:
            mode = self.mode_for_message(text)
        return self.profile.prompt_block(mode)
