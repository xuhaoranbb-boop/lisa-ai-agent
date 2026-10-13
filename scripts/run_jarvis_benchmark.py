from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.jarvis_benchmark.cases import (  # noqa: E402
    agent_execution_case,
    context_compression_case,
    memory_retrieval_case,
    recovery_case,
    security_case,
    semantic_fallback_case,
    token_budget_case,
    tool_registry_case,
)
from tests.jarvis_benchmark.runner import BenchmarkRunner  # noqa: E402

BRIDGE_URL = "http://127.0.0.1:8765"


async def voice_case(text: str) -> dict[str, object]:
    first_text: float | None = None
    first_audio: float | None = None
    done: float | None = None
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream(
            "POST",
            f"{BRIDGE_URL}/api/respond/stream",
            json={
                "text": text,
                "request_id": f"benchmark-{time.time_ns()}",
                "session_id": "benchmark-session",
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                event = json.loads(line)
                if event.get("type") == "text_delta" and first_text is None:
                    first_text = time.perf_counter()
                elif event.get("type") == "audio_chunk" and first_audio is None:
                    first_audio = time.perf_counter()
                elif event.get("type") == "done":
                    done = time.perf_counter()
    if done is None:
        raise RuntimeError("Voice response did not complete.")
    return {
        "first_text_ms": round(((first_text or done) - started) * 1_000, 3),
        "first_audio_ms": round(((first_audio or done) - started) * 1_000, 3),
        "total_ms": round((done - started) * 1_000, 3),
    }


async def browser_case() -> dict[str, object]:
    from jarvis.runtime.browser import BrowserTool, PlaywrightBrowserAdapter

    tool = BrowserTool(PlaywrightBrowserAdapter())
    try:
        result = await tool.open("https://example.com")
        return {
            "status": result["status"],
            "title": result["title"],
        }
    finally:
        await tool.close()


async def long_running_probe(seconds: int = 5) -> dict[str, object]:
    started = time.perf_counter()
    iterations = 0
    while time.perf_counter() - started < seconds:
        await asyncio.sleep(0.05)
        iterations += 1
    return {"seconds": seconds, "iterations": iterations}


def score(summary: dict[str, object]) -> dict[str, object]:
    categories = summary.get("categories", {})
    weights = {
        "voice": 10,
        "memory": 10,
        "context": 10,
        "agent": 15,
        "tools": 10,
        "vision": 10,
        "recovery": 10,
        "security": 10,
        "performance": 10,
        "stability": 5,
    }
    earned = 0.0
    detail: dict[str, float] = {}
    for category, weight in weights.items():
        bucket = categories.get(category)
        rate = float(bucket["pass_rate"]) if bucket else 0.0
        points = weight * rate
        detail[category] = round(points, 2)
        earned += points
    return {
        "score": round(earned, 2),
        "maximum": 100,
        "breakdown": detail,
    }


async def main() -> int:
    runner = BenchmarkRunner()
    await runner.run_case(
        "memory-100",
        "memory",
        lambda: memory_retrieval_case(100),
    )
    await runner.run_case(
        "memory-1000",
        "memory",
        lambda: memory_retrieval_case(1_000),
    )
    await runner.run_case(
        "memory-10000",
        "memory",
        lambda: memory_retrieval_case(10_000),
    )
    await runner.run_case(
        "semantic-fallback",
        "memory",
        semantic_fallback_case,
    )
    await runner.run_case(
        "context-compression",
        "context",
        context_compression_case,
    )
    await runner.run_case(
        "agent-execution",
        "agent",
        agent_execution_case,
    )
    await runner.run_case(
        "tool-registry",
        "tools",
        tool_registry_case,
    )
    await runner.run_case(
        "token-budget",
        "performance",
        token_budget_case,
    )
    await runner.run_case("security-permission", "security", security_case)
    await runner.run_case("recovery-mcp", "recovery", recovery_case)
    await runner.run_case("browser-open", "vision", browser_case)
    try:
        await runner.run_case(
            "voice-normal",
            "voice",
            lambda: voice_case("用一句话回答。"),
        )
        await runner.run_case(
            "voice-long",
            "voice",
            lambda: voice_case("请用两句简短说明流式语音的优势。"),
        )
    except Exception:
        pass
    await runner.run_case("long-running-5s", "stability", long_running_probe)
    soak_evidence = _load_soak_evidence()
    for seconds in (30 * 60, 60 * 60, 120 * 60):
        checkpoint = soak_evidence.get(str(seconds // 60))
        if checkpoint is not None and checkpoint.get("passed"):
            await runner.run_case(
                f"long-running-{seconds // 60}m",
                "stability",
                _soak_evidence_case(checkpoint),
            )
        else:
            await runner.run_case(
                f"long-running-{seconds // 60}m",
                "stability",
                _blocked_case(
                    f"BLOCKED: no passing {seconds // 60}-minute soak evidence."
                ),
            )

    output_dir = ROOT / "benchmark" / "results"
    paths = runner.write(output_dir)
    summary = runner.summary()
    score_data = score(summary)
    score_path = ROOT / "JARVIS_SCORE.md"
    score_path.write_text(
        "# JARVIS Score\n\n"
        f"- Total: {score_data['score']} / {score_data['maximum']}\n\n"
        + "\n".join(
            f"- {name}: {points}"
            for name, points in score_data["breakdown"].items()
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "summary": summary,
                "score": score_data,
                "outputs": {key: str(value) for key, value in paths.items()},
                "score_path": str(score_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _blocked_case(message: str):
    async def blocked() -> dict[str, object]:
        raise RuntimeError(message)

    return blocked


def _soak_evidence_case(checkpoint: dict[str, object]):
    async def evidence_case() -> dict[str, object]:
        return {
            "observed_minutes": checkpoint.get("observed_minutes"),
            "samples": checkpoint.get("samples"),
            "p95_ms": checkpoint.get("p95_ms"),
            "max_memory_percent": checkpoint.get("max_memory_percent"),
            "restart_count": checkpoint.get("restart_count"),
        }

    return evidence_case


def _load_soak_evidence() -> dict[str, dict[str, object]]:
    path = ROOT / "benchmark" / "results" / "soak_latest.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not payload.get("complete"):
        return {}
    checkpoints = payload.get("checkpoints")
    if not isinstance(checkpoints, dict):
        return {}
    return {
        str(key): dict(value)
        for key, value in checkpoints.items()
        if isinstance(value, dict)
    }

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
