from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BenchmarkCase = Callable[[], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class BenchmarkResult:
    test_name: str
    category: str
    success: bool
    latency_ms: float
    tokens: int = 0
    cost: float = 0.0
    error: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_name": self.test_name,
            "category": self.category,
            "success": self.success,
            "latency_ms": self.latency_ms,
            "tokens": self.tokens,
            "cost": self.cost,
            "error": self.error,
            "timestamp": self.timestamp,
            "details": self.details,
        }


class BenchmarkRunner:
    def __init__(self, name: str = "JARVIS Benchmark") -> None:
        self.name = name
        self.results: list[BenchmarkResult] = []

    async def run_case(
        self,
        test_name: str,
        category: str,
        case: BenchmarkCase,
    ) -> BenchmarkResult:
        started = time.perf_counter()
        try:
            details = await case() or {}
            result = BenchmarkResult(
                test_name=test_name,
                category=category,
                success=True,
                latency_ms=round((time.perf_counter() - started) * 1_000, 3),
                tokens=int(details.get("tokens") or 0),
                cost=float(details.get("cost") or 0.0),
                details=details,
            )
        except Exception as exc:
            result = BenchmarkResult(
                test_name=test_name,
                category=category,
                success=False,
                latency_ms=round((time.perf_counter() - started) * 1_000, 3),
                error=f"{type(exc).__name__}: {exc}",
            )
        self.results.append(result)
        return result

    def summary(self) -> dict[str, Any]:
        totals = len(self.results)
        passed = sum(1 for result in self.results if result.success)
        latencies = [result.latency_ms for result in self.results]
        categories: dict[str, dict[str, Any]] = {}
        for result in self.results:
            bucket = categories.setdefault(
                result.category,
                {"total": 0, "passed": 0, "latency_ms": []},
            )
            bucket["total"] += 1
            bucket["passed"] += int(result.success)
            bucket["latency_ms"].append(result.latency_ms)
        for bucket in categories.values():
            bucket["pass_rate"] = bucket["passed"] / max(bucket["total"], 1)
            bucket["p50_ms"] = percentile(bucket["latency_ms"], 50)
            bucket["p95_ms"] = percentile(bucket["latency_ms"], 95)
            bucket["p99_ms"] = percentile(bucket["latency_ms"], 99)
            del bucket["latency_ms"]
        return {
            "name": self.name,
            "total": totals,
            "passed": passed,
            "failed": totals - passed,
            "pass_rate": passed / max(totals, 1),
            "p50_ms": percentile(latencies, 50),
            "p90_ms": percentile(latencies, 90),
            "p95_ms": percentile(latencies, 95),
            "p99_ms": percentile(latencies, 99),
            "total_tokens": sum(result.tokens for result in self.results),
            "estimated_cost": round(
                sum(result.cost for result in self.results),
                6,
            ),
            "categories": categories,
        }

    def write(self, output_dir: Path) -> dict[str, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / "jarvis_benchmark.json"
        markdown_path = output_dir / "JARVIS_BENCHMARK_REPORT.md"
        payload = {
            "summary": self.summary(),
            "results": [result.to_dict() for result in self.results],
        }
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        markdown_path.write_text(self.markdown(), encoding="utf-8")
        return {"json": json_path, "markdown": markdown_path}

    def markdown(self) -> str:
        summary = self.summary()
        lines = [
            "# JARVIS Benchmark Report",
            "",
            f"- Total: {summary['total']}",
            f"- Passed: {summary['passed']}",
            f"- Failed: {summary['failed']}",
            f"- Pass rate: {summary['pass_rate']:.1%}",
            f"- P50: {summary['p50_ms']:.1f} ms",
            f"- P90: {summary['p90_ms']:.1f} ms",
            f"- P95: {summary['p95_ms']:.1f} ms",
            f"- P99: {summary['p99_ms']:.1f} ms",
            "",
            "| Test | Category | Result | Latency | Error |",
            "| --- | --- | --- | ---: | --- |",
        ]
        for result in self.results:
            lines.append(
                f"| {result.test_name} | {result.category} | "
                f"{'PASS' if result.success else 'FAIL'} | "
                f"{result.latency_ms:.1f} ms | {result.error} |"
            )
        return "\n".join(lines) + "\n"


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = (len(ordered) - 1) * percent / 100
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return round(
        ordered[lower] + (ordered[upper] - ordered[lower]) * fraction,
        3,
    )
