from __future__ import annotations

from jarvis.runtime.contracts import LLMProvider


class VisionAnalyzer:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    async def describe(
        self,
        image: bytes,
        prompt: str = "Describe the current screen and identify actionable UI elements.",
    ) -> str:
        reply = await self.provider.vision(
            [{"role": "user", "content": prompt}],
            image,
        )
        return reply.content
