from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from jarvis.runtime.contracts import PermissionLevel, ToolSpec
from jarvis.runtime.desktop import DesktopController, DesktopObservation
from jarvis.runtime.file_intelligence import FileIntelligence
from jarvis.runtime.tooling import RuntimeTool, ToolRegistry


class Modality(StrEnum):
    VOICE = "VOICE"
    TEXT = "TEXT"
    IMAGE = "IMAGE"
    SCREEN = "SCREEN"
    FILE = "FILE"
    VISION = "VISION"


@dataclass(frozen=True)
class ScreenElement:
    name: str
    control_type: str
    automation_id: str
    bounds: dict[str, int]
    enabled: bool
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "control_type": self.control_type,
            "automation_id": self.automation_id,
            "bounds": self.bounds,
            "enabled": self.enabled,
            "confidence": self.confidence,
        }


@dataclass
class ScreenState:
    application: str
    window: str
    elements: list[ScreenElement]
    text: str
    buttons: list[str]
    dialogs: list[str]
    errors: list[str]
    screenshot_hash: str
    captured_at: float
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "application": self.application,
            "window": self.window,
            "elements": [element.to_dict() for element in self.elements],
            "text": self.text,
            "buttons": self.buttons,
            "dialogs": self.dialogs,
            "errors": self.errors,
            "screenshot_hash": self.screenshot_hash,
            "captured_at": self.captured_at,
            "confidence": self.confidence,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class MultimodalResult:
    modality: Modality
    text: str
    data: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "modality": str(self.modality),
            "text": self.text,
            "data": self.data,
            "confidence": self.confidence,
        }


class ScreenObserver:
    """Capture screen state and re-run vision only when the screen changes."""

    def __init__(
        self,
        controller: DesktopController,
        provider: Any | None = None,
        *,
        cache_seconds: float = 8.0,
    ) -> None:
        self.controller = controller
        self.provider = provider
        self.cache_seconds = max(0.5, cache_seconds)
        self._cache: ScreenState | None = None

    async def observe(self, *, force: bool = False) -> ScreenState:
        observation = await self.controller.observe(include_screenshot=True)
        screenshot_hash = _screenshot_hash(observation)
        now = time.monotonic()
        if (
            not force
            and self._cache is not None
            and self._cache.screenshot_hash == screenshot_hash
            and now - self._cache.captured_at < self.cache_seconds
        ):
            return self._cache

        ui_tree = await self.controller.adapter.ui_tree(180)
        elements = [
            ScreenElement(
                name=str(item.get("name") or ""),
                control_type=str(item.get("controlType") or ""),
                automation_id=str(item.get("automationId") or ""),
                bounds=dict(item.get("bounds") or {}),
                enabled=bool(item.get("enabled", True)),
                confidence=0.98,
            )
            for item in ui_tree
        ]
        windows = observation.windows
        active = windows[0] if windows else {}
        vision_text = ""
        confidence = 0.75
        if self.provider is not None and observation.screenshot:
            try:
                reply = await self.provider.vision(
                    [
                        {
                            "role": "system",
                            "content": (
                                "Describe the screen state as JSON with keys "
                                "text, dialogs, errors, buttons, and confidence."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                "Analyze the screenshot and identify visible UI "
                                "elements and errors. Return JSON only."
                            ),
                        },
                    ],
                    observation.screenshot,
                )
                parsed = _parse_json_object(reply.content)
                vision_text = str(parsed.get("text") or reply.content)
                confidence = float(parsed.get("confidence") or 0.75)
            except Exception:
                vision_text = ""

        element_names = [element.name for element in elements if element.name]
        buttons = [
            element.name
            for element in elements
            if "button" in element.control_type.lower() and element.name
        ]
        dialogs = [
            element.name
            for element in elements
            if "dialog" in element.control_type.lower() and element.name
        ]
        errors = [
            name
            for name in element_names
            if any(term in name.lower() for term in ("error", "错误", "失败", "exception"))
        ]
        state = ScreenState(
            application=str(active.get("ProcessName") or ""),
            window=str(active.get("MainWindowTitle") or ""),
            elements=elements,
            text=vision_text or "\n".join(element_names[:120]),
            buttons=buttons,
            dialogs=dialogs,
            errors=errors,
            screenshot_hash=screenshot_hash,
            captured_at=now,
            confidence=confidence,
            metadata={"window_count": len(windows)},
        )
        self._cache = state
        return state


class VisionAgent:
    def __init__(
        self,
        observer: ScreenObserver,
        provider: Any | None = None,
    ) -> None:
        self.observer = observer
        self.provider = provider or observer.provider

    async def analyze_screen(self, question: str) -> MultimodalResult:
        state = await self.observer.observe()
        return MultimodalResult(
            modality=Modality.SCREEN,
            text=state.text,
            data=state.to_dict(),
            confidence=state.confidence,
        )

    async def locate(self, description: str) -> MultimodalResult:
        state = await self.observer.observe()
        candidates = [
            element
            for element in state.elements
            if description.lower() in element.name.lower()
        ]
        if candidates:
            element = candidates[0]
            return MultimodalResult(
                modality=Modality.SCREEN,
                text=element.name,
                data={
                    "element": element.to_dict(),
                    "strategy": "uia",
                },
                confidence=element.confidence,
            )
        return MultimodalResult(
            modality=Modality.SCREEN,
            text="",
            data={"strategy": "vision", "screen": state.to_dict()},
            confidence=0.3,
        )

    async def verify_action(self, goal: str) -> MultimodalResult:
        """Use the current screen to verify whether a desktop action worked."""
        state = await self.observer.observe()
        if self.provider is None:
            return MultimodalResult(
                modality=Modality.SCREEN,
                text="",
                data={
                    "verified": False,
                    "reason": "Vision provider is not configured.",
                    "screen": state.to_dict(),
                },
                confidence=0.0,
            )
        reply = await self.provider.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Verify whether the intended desktop action succeeded. "
                        "Return only JSON with keys verified (boolean) and reason "
                        "(one short sentence). Do not invent success."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Intended action: {goal}\n"
                        f"Current screen text:\n{state.text[:6_000]}"
                    ),
                },
            ],
            [],
        )
        try:
            payload = json.loads(reply.content)
            verified = bool(payload.get("verified"))
            reason = str(payload.get("reason") or "")
        except Exception:
            lowered = reply.content.lower()
            verified = "true" in lowered and "false" not in lowered
            reason = reply.content[:500]
        return MultimodalResult(
            modality=Modality.SCREEN,
            text=reason,
            data={
                "verified": verified,
                "reason": reason,
                "screen": state.to_dict(),
            },
            confidence=0.8 if verified else 0.5,
        )

    def register(self, registry: ToolRegistry) -> list[str]:
        async def observe_screen() -> dict[str, Any]:
            return (await self.observer.observe()).to_dict()

        async def analyze_screen(question: str) -> dict[str, Any]:
            return (await self.analyze_screen(question)).to_dict()

        async def locate_element(description: str) -> dict[str, Any]:
            return (await self.locate(description)).to_dict()

        async def verify_action(goal: str) -> dict[str, Any]:
            return (await self.verify_action(goal)).to_dict()

        definitions = [
            (
                "screen_observe",
                "Observe the current screen and return structured screen state.",
                {},
                [],
                observe_screen,
            ),
            (
                "vision_analyze_screen",
                "Analyze the current screen and answer a question about it.",
                {"question": {"type": "string"}},
                ["question"],
                analyze_screen,
            ),
            (
                "vision_locate_element",
                "Locate a visible UI element using UIA first, vision fallback second.",
                {"description": {"type": "string"}},
                ["description"],
                locate_element,
            ),
            (
                "vision_verify_action",
                "Verify whether an important desktop action actually succeeded.",
                {"goal": {"type": "string"}},
                ["goal"],
                verify_action,
            ),
        ]
        names: list[str] = []
        for name, description, properties, required, handler in definitions:
            registry.register(
                RuntimeTool(
                    spec=ToolSpec(
                        name=name,
                        description=description,
                        parameters={
                            "type": "object",
                            "properties": properties,
                            "required": required,
                            "additionalProperties": False,
                        },
                        permission=PermissionLevel.READ_ONLY,
                        capability="vision",
                        category="VISION",
                        risk_level=PermissionLevel.READ_ONLY,
                    ),
                    handler=handler,
                )
            )
            names.append(name)
        return names


class MultimodalEngine:
    """Normalize text, image, screen, and file inputs into one representation."""

    def __init__(
        self,
        *,
        vision_agent: VisionAgent | None = None,
        file_intelligence: FileIntelligence | None = None,
        provider: Any | None = None,
    ) -> None:
        self.vision_agent = vision_agent
        self.file_intelligence = file_intelligence or FileIntelligence(provider)
        self.provider = provider

    async def process(
        self,
        *,
        text: str = "",
        image: bytes | None = None,
        file_paths: list[Path] | None = None,
        include_screen: bool = False,
    ) -> list[MultimodalResult]:
        results: list[MultimodalResult] = []
        if text.strip():
            results.append(
                MultimodalResult(
                    modality=Modality.TEXT,
                    text=text.strip(),
                    confidence=1.0,
                )
            )
        if image and self.provider is not None:
            reply = await self.provider.vision(
                [
                    {
                        "role": "system",
                        "content": "Describe and interpret the attached image accurately.",
                    },
                    {"role": "user", "content": "Analyze this image."},
                ],
                image,
            )
            results.append(
                MultimodalResult(
                    modality=Modality.IMAGE,
                    text=reply.content,
                    data={"bytes": len(image)},
                    confidence=0.8,
                )
            )
        if include_screen and self.vision_agent is not None:
            results.append(await self.vision_agent.analyze_screen(text))
        for path in file_paths or []:
            insight = await self.file_intelligence.inspect(path)
            results.append(
                MultimodalResult(
                    modality=Modality.FILE,
                    text=insight.summary,
                    data=insight.to_dict(),
                    confidence=0.9,
                )
            )
        return results


class AmbientContext:
    """Current world state assembled for the Agent."""

    def __init__(
        self,
        *,
        screen_observer: ScreenObserver | None = None,
        current_file: Path | None = None,
    ) -> None:
        self.screen_observer = screen_observer
        self.current_file = current_file
        self.recent_actions: list[dict[str, Any]] = []

    def record_action(self, action: str, detail: dict[str, Any] | None = None) -> None:
        self.recent_actions.append(
            {
                "action": action,
                "detail": detail or {},
                "at": time.time(),
            }
        )
        self.recent_actions = self.recent_actions[-20:]

    async def snapshot(self, *, include_screen: bool = False) -> dict[str, Any]:
        screen = (
            (await self.screen_observer.observe()).to_dict()
            if include_screen and self.screen_observer is not None
            else None
        )
        return {
            "time": time.time(),
            "screen": screen,
            "current_file": str(self.current_file) if self.current_file else None,
            "recent_actions": self.recent_actions[-10:],
        }


def _screenshot_hash(observation: DesktopObservation) -> str:
    if not observation.screenshot:
        return "no-screenshot"
    return hashlib.sha256(observation.screenshot).hexdigest()


def _parse_json_object(value: str) -> dict[str, Any]:
    match = json.loads(value)
    return match if isinstance(match, dict) else {}
