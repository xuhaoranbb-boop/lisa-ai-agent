from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from jarvis.memory import Memory, MemoryStore, Message
from jarvis.persona import PersonaManager
from jarvis.runtime.semantic_memory import MemoryEmbeddingStore
from jarvis.runtime.tasks import Task, TaskEngine, TaskStatus


@dataclass(frozen=True)
class ResolvedReference:
    phrase: str
    target_type: str
    target_id: str
    value: str
    confidence: float


@dataclass(frozen=True)
class PendingAction:
    id: str
    action: str
    description: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContextSnapshot:
    session_id: str
    owner_id: str
    query: str
    resolved_query: str
    goal: str
    task: Task | None
    history: list[Message]
    memories: list[Memory]
    preferences: list[Memory]
    recent_tool_results: list[dict[str, Any]]
    resolved_references: list[ResolvedReference]
    unresolved_references: list[ResolvedReference]
    pending_actions: list[PendingAction]
    persona_prompt: str
    summary: str
    covered_message_id: int
    world_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "query": self.query,
            "resolved_query": self.resolved_query,
            "goal": self.goal,
            "task": self.task.to_dict() if self.task is not None else None,
            "history": [
                {
                    "id": message.id,
                    "role": message.role,
                    "content": message.content,
                    "created_at": message.created_at,
                }
                for message in self.history
            ],
            "memories": [_memory_to_dict(memory) for memory in self.memories],
            "preferences": [_memory_to_dict(memory) for memory in self.preferences],
            "recent_tool_results": self.recent_tool_results,
            "resolved_references": [
                {
                    "phrase": item.phrase,
                    "target_type": item.target_type,
                    "target_id": item.target_id,
                    "value": item.value,
                    "confidence": item.confidence,
                }
                for item in self.resolved_references
            ],
            "unresolved_references": [
                {
                    "phrase": item.phrase,
                    "target_type": item.target_type,
                    "target_id": item.target_id,
                    "value": item.value,
                    "confidence": item.confidence,
                }
                for item in self.unresolved_references
            ],
            "pending_actions": [
                {
                    "id": action.id,
                    "action": action.action,
                    "description": action.description,
                    "metadata": action.metadata,
                }
                for action in self.pending_actions
            ],
            "persona_prompt": self.persona_prompt,
            "summary": self.summary,
            "covered_message_id": self.covered_message_id,
            "world_state": self.world_state,
        }


class ReferenceResolver:
    """Resolve short conversational references against recent context."""

    def resolve_reference(
        self,
        query: str,
        *,
        history: list[Message],
        task: Task | None = None,
        tool_results: list[dict[str, Any]] | None = None,
    ) -> ResolvedReference | None:
        normalized = query.strip().lower()
        if not normalized:
            return None

        if any(term in normalized for term in ("继续", "接着", "continue")) and task:
            pending = next(
                (
                    step
                    for step in task.plan
                    if step.status
                    not in {
                        TaskStatus.COMPLETED,
                        TaskStatus.FAILED,
                        TaskStatus.CANCELLED,
                    }
                ),
                None,
            )
            return ResolvedReference(
                phrase=query,
                target_type="task",
                target_id=task.id,
                value=pending.title if pending else task.goal,
                confidence=0.95,
            )

        ordinal_match = any(
            term in normalized
            for term in ("第一个", "第二个", "第三个", "第1个", "第2个", "第3个")
        )
        if ordinal_match and task and task.plan:
            index = 0
            if "第二" in normalized or "第2" in normalized:
                index = 1
            elif "第三" in normalized or "第3" in normalized:
                index = 2
            if index < len(task.plan):
                step = task.plan[index]
                return ResolvedReference(
                    phrase=query,
                    target_type="task_step",
                    target_id=step.id,
                    value=step.title,
                    confidence=0.9,
                )

        if "刚才的项目" in normalized or "之前那个项目" in normalized:
            for message in reversed(history):
                if "项目" in message.content:
                    return ResolvedReference(
                        phrase=query,
                        target_type="project",
                        target_id=str(message.id),
                        value=message.content[:500],
                        confidence=0.8,
                    )
        if "刚才的方案" in normalized or "之前那个方案" in normalized:
            for message in reversed(history):
                if "方案" in message.content:
                    return ResolvedReference(
                        phrase=query,
                        target_type="plan",
                        target_id=str(message.id),
                        value=message.content[:500],
                        confidence=0.8,
                    )

        if any(
            term in normalized
            for term in (
                "这个",
                "那个",
                "刚才",
                "上一个",
                "之前那个",
                "换一个",
            )
        ):
            for result in reversed(tool_results or []):
                value = str(result.get("summary") or result.get("result") or "").strip()
                if not value:
                    continue
                return ResolvedReference(
                    phrase=query,
                    target_type="tool_result",
                    target_id=str(result.get("tool") or "latest"),
                    value=value[:500],
                    confidence=0.78,
                )
            for message in reversed(history):
                if message.role != "assistant" or not message.content.strip():
                    continue
                return ResolvedReference(
                    phrase=query,
                    target_type="assistant_message",
                    target_id=str(message.id),
                    value=message.content[:500],
                    confidence=0.72,
                )

        return None


class ContextEngine:
    """Build one bounded context snapshot for every Agent turn."""

    def __init__(
        self,
        memory_store: MemoryStore,
        task_engine: TaskEngine,
        persona_manager: PersonaManager,
        *,
        semantic_store: MemoryEmbeddingStore | None = None,
        provider: Any | None = None,
        ambient_context: Any | None = None,
        max_context_chars: int = 18_000,
    ) -> None:
        self.memory_store = memory_store
        self.task_engine = task_engine
        self.persona_manager = persona_manager
        self.semantic_store = semantic_store
        self.provider = provider
        self.ambient_context = ambient_context
        self.max_context_chars = max(2_000, max_context_chars)
        self.reference_resolver = ReferenceResolver()
        self._tool_results: dict[str, deque[dict[str, Any]]] = {}
        self._pending_actions: dict[str, list[PendingAction]] = {}

    def get_current_task(
        self,
        session_id: str,
        task_id: str | None = None,
    ) -> Task | None:
        if task_id:
            task = self.task_engine.get_task(task_id)
            if task is not None:
                return task
        return next(
            (
                task
                for task in self.task_engine.list_tasks(50)
                if task.session_id == session_id
                and task.status
                not in {
                    TaskStatus.COMPLETED,
                    TaskStatus.CANCELLED,
                    TaskStatus.FAILED,
                }
            ),
            None,
        )

    def get_current_goal(self, session_id: str, task_id: str | None = None) -> str:
        task = self.get_current_task(session_id, task_id)
        return task.goal if task is not None else ""

    async def build_context(
        self,
        query: str,
        *,
        session_id: str,
        task_id: str | None = None,
        owner_id: str = "local",
    ) -> ContextSnapshot:
        task = self.get_current_task(session_id, task_id)
        history = self.memory_store.history(session_id, 200)
        tool_results = list(self._tool_results.get(session_id, ()))
        reference = self.reference_resolver.resolve_reference(
            query,
            history=history,
            task=task,
            tool_results=tool_results,
        )
        resolved_query = query
        resolved: list[ResolvedReference] = []
        unresolved: list[ResolvedReference] = []
        if reference is not None:
            resolved_query = (
                f"{query}\nResolved reference: {reference.value} "
                f"({reference.target_type}, confidence={reference.confidence:.2f})"
            )
            resolved.append(reference)
        elif self._looks_referential(query):
            unresolved.append(
                ResolvedReference(
                    phrase=query,
                    target_type="unresolved",
                    target_id="",
                    value="",
                    confidence=0.0,
                )
            )

        summary, covered = await self.compact_context(
            session_id,
            query=query,
            task=task,
        )
        world_state = (
            await self.ambient_context.snapshot()
            if self.ambient_context is not None
            else {}
        )
        raw_history = (
            self.memory_store.history_after(session_id, covered, 100)
            if covered
            else history
        )
        bounded_history = self._bounded_history(
            raw_history,
            max(2_000, self.max_context_chars // 3),
        )
        memories = await self._retrieve_memories(
            resolved_query,
            owner_id=owner_id,
            session_id=session_id,
        )
        preferences = [
            memory
            for memory in memories
            if memory.category in {"preference", "persona_preference"}
        ]
        return ContextSnapshot(
            session_id=session_id,
            owner_id=owner_id,
            query=query,
            resolved_query=resolved_query,
            goal=task.goal if task is not None else "",
            task=task,
            history=bounded_history,
            memories=memories,
            preferences=preferences,
            recent_tool_results=tool_results,
            resolved_references=resolved,
            unresolved_references=unresolved,
            pending_actions=list(self._pending_actions.get(session_id, ())),
            persona_prompt=self.persona_manager.prompt_for(query),
            summary=summary,
            covered_message_id=covered,
            world_state=world_state,
        )

    async def resolve_context(
        self,
        query: str,
        *,
        session_id: str,
        task_id: str | None = None,
        owner_id: str = "local",
    ) -> ContextSnapshot:
        return await self.build_context(
            query,
            session_id=session_id,
            task_id=task_id,
            owner_id=owner_id,
        )

    async def compact_context(
        self,
        session_id: str,
        *,
        query: str = "",
        task: Task | None = None,
    ) -> tuple[str, int]:
        history = self.memory_store.history(session_id, 200)
        if not history:
            return "", 0
        total_chars = sum(len(message.content) for message in history)
        existing = self.memory_store.get_session_summary(session_id)
        if total_chars <= self.max_context_chars:
            return existing or ("", 0)

        summary = await self._summarize_history(
            history,
            query=query,
            task=task,
        )
        summary = summary[:6_000]
        covered = history[-1].id
        self.memory_store.save_session_summary(session_id, summary, covered)
        return summary, covered

    def record_tool_result(
        self,
        session_id: str,
        tool: str,
        result: Any,
    ) -> None:
        queue = self._tool_results.setdefault(session_id, deque(maxlen=12))
        text = (
            result
            if isinstance(result, str)
            else json.dumps(result, ensure_ascii=False, default=str)
        )
        queue.append(
            {
                "tool": tool,
                "summary": text[:800],
                "result": text[:2_000],
            }
        )

    def add_pending_action(
        self,
        session_id: str,
        action: PendingAction,
    ) -> None:
        self._pending_actions.setdefault(session_id, []).append(action)

    def resolve_pending_action(self, session_id: str, action_id: str) -> bool:
        pending = self._pending_actions.get(session_id, [])
        remaining = [item for item in pending if item.id != action_id]
        self._pending_actions[session_id] = remaining
        return len(remaining) != len(pending)

    async def _retrieve_memories(
        self,
        query: str,
        *,
        owner_id: str,
        session_id: str,
    ) -> list[Memory]:
        if self.semantic_store is not None:
            memories = await self.semantic_store.search(
                query,
                6,
                owner_id=owner_id,
                session_id=session_id,
            )
            if memories:
                return memories
        return self.memory_store.recall_for_context(
            query,
            6,
            owner_id=owner_id,
            session_id=session_id,
        )

    @staticmethod
    def _looks_referential(query: str) -> bool:
        return any(
            term in query
            for term in (
                "这个",
                "那个",
                "刚才",
                "上一个",
                "之前",
                "第一个",
                "第二个",
                "换一个",
            )
        )

    @staticmethod
    def _bounded_history(
        history: list[Message],
        max_chars: int,
    ) -> list[Message]:
        selected: list[Message] = []
        used = 0
        for message in reversed(history):
            size = len(message.content)
            if selected and used + size > max_chars:
                break
            selected.append(message)
            used += size
        selected.reverse()
        return selected

    async def _summarize_history(
        self,
        history: list[Message],
        *,
        query: str,
        task: Task | None,
    ) -> str:
        transcript = "\n".join(
            f"{message.role}: {message.content}"
            for message in history[-100:]
        )
        task_state = (
            f"{task.goal} | status={task.status} | progress={task.progress:.0%}"
            if task is not None
            else "no active task"
        )
        if self.provider is not None:
            try:
                reply = await self.provider.complete(
                    [
                        {
                            "role": "system",
                            "content": (
                                "Compress this conversation into a durable working summary. "
                                "Preserve user goals, current task, important decisions, "
                                "preferences, key facts, unresolved references, and unfinished "
                                "work. Remove repetition. Return plain text only."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Current query: {query}\n"
                                f"Task: {task_state}\n\nConversation:\n{transcript}"
                            ),
                        },
                    ],
                    [],
                )
                if reply.content.strip():
                    return reply.content.strip()
            except Exception:
                pass
        last_messages = history[-8:]
        lines = [f"Current task: {task_state}"]
        for message in last_messages:
            lines.append(f"{message.role}: {message.content[:400]}")
        return "\n".join(lines)


def _memory_to_dict(memory: Memory) -> dict[str, Any]:
    return {
        "id": memory.id,
        "content": memory.content,
        "category": memory.category,
        "importance": memory.importance,
        "confidence": memory.confidence,
        "summary": memory.summary,
        "metadata": memory.metadata or {},
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
        "last_used_at": memory.last_used_at,
        "session_id": memory.session_id,
        "owner_id": memory.owner_id,
    }
