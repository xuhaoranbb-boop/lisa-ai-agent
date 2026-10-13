from __future__ import annotations

import json
import re
import uuid
from typing import Protocol

from jarvis.runtime.contracts import LLMProvider
from jarvis.runtime.tasks import TaskStep


class Planner(Protocol):
    async def create_plan(self, goal: str, context: str = "") -> list[TaskStep]: ...


class LLMPlanner:
    """Ask a reasoning model for a short verifiable plan."""

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    async def create_plan(self, goal: str, context: str = "") -> list[TaskStep]:
        prompt = (
            "Create a short execution plan for the user's goal. "
            "Return only a JSON array. Each item must contain title, tool, and status. "
            "Use at most 8 steps. Do not include hidden reasoning.\n\n"
            f"Goal: {goal}\nContext: {context}"
        )
        reply = await self.provider.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "You are the planning layer of a personal AI butler. "
                        "Return JSON only."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            [],
        )
        parsed = _parse_plan(reply.content)
        if parsed:
            return parsed
        return [
            TaskStep(
                id=uuid.uuid4().hex,
                title=goal,
                tool="",
            )
        ]


def _parse_plan(content: str) -> list[TaskStep]:
    text = content.strip()
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        text = match.group(0)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []

    steps: list[TaskStep] = []
    for item in value[:8]:
        if isinstance(item, str):
            title = item
            tool = ""
        elif isinstance(item, dict):
            title = str(item.get("title") or item.get("step") or "").strip()
            tool = str(item.get("tool") or "").strip()
        else:
            continue
        if not title:
            continue
        steps.append(
            TaskStep(
                id=uuid.uuid4().hex,
                title=title,
                tool=tool,
            )
        )
    return steps
