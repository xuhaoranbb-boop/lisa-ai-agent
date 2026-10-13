from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "benchmark" / "results" / "soak_latest.json"


async def run_soak(
    *,
    base_url: str,
    minutes: int,
    checkpoints: tuple[int, ...],
    interval_seconds: float,
    output: Path,
) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    started = time.monotonic()
    total_seconds = minutes * 60
    checkpoint_seconds = {
        checkpoint: checkpoint * 60 for checkpoint in checkpoints if checkpoint <= minutes
    }
    completed: dict[int, dict[str, Any]] = {}
    latencies: list[float] = []
    errors: list[dict[str, Any]] = []
    health_states: list[str] = []
    memory_samples: list[float] = []
    last_restart_count = 0
    last_progress_second = -1

    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= total_seconds:
                break
            sample_started = time.perf_counter()
            try:
                health_response = await client.get(f"{base_url}/health/details")
                health_response.raise_for_status()
                health = health_response.json()
                production_response = await client.get(f"{base_url}/api/production")
                production_response.raise_for_status()
                production = production_response.json()["production"]
            except Exception as exc:
                errors.append(
                    {
                        "elapsed_seconds": round(elapsed, 3),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            else:
                latencies.append(round((time.perf_counter() - sample_started) * 1_000, 3))
                health_status = str(health.get("health", {}).get("status") or "UNKNOWN")
                health_states.append(health_status)
                memory_percent = health.get("system", {}).get("memory_percent")
                if isinstance(memory_percent, (int, float)):
                    memory_samples.append(float(memory_percent))
                services = production.get("services") or []
                last_restart_count = sum(
                    int(service.get("restart_count") or 0) for service in services
                )

            elapsed = time.monotonic() - started
            progress_second = int(elapsed)
            if progress_second // 60 != last_progress_second:
                last_progress_second = progress_second // 60
                print(
                    json.dumps(
                        {
                            "elapsed_minutes": round(elapsed / 60, 2),
                            "samples": len(latencies),
                            "errors": len(errors),
                            "checkpoints": sorted(completed),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

            for checkpoint, threshold in checkpoint_seconds.items():
                if checkpoint in completed or elapsed < threshold:
                    continue
                checkpoint_result = _checkpoint_result(
                    checkpoint=checkpoint,
                    elapsed_seconds=elapsed,
                    latencies=latencies,
                    errors=errors,
                    health_states=health_states,
                    memory_samples=memory_samples,
                    restart_count=last_restart_count,
                )
                completed[checkpoint] = checkpoint_result
                _write_result(
                    output,
                    started_at=started_at,
                    requested_minutes=minutes,
                    elapsed_seconds=elapsed,
                    completed=completed,
                    latencies=latencies,
                    errors=errors,
                    health_states=health_states,
                    memory_samples=memory_samples,
                    restart_count=last_restart_count,
                    complete=False,
                )
                print(
                    json.dumps(
                        {
                            "checkpoint_minutes": checkpoint,
                            "passed": checkpoint_result["passed"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

            remaining = max(0.0, interval_seconds - (time.monotonic() - started - elapsed))
            await asyncio.sleep(remaining)

    elapsed = time.monotonic() - started
    final = _checkpoint_result(
        checkpoint=minutes,
        elapsed_seconds=elapsed,
        latencies=latencies,
        errors=errors,
        health_states=health_states,
        memory_samples=memory_samples,
        restart_count=last_restart_count,
    )
    completed[minutes] = final
    payload = _write_result(
        output,
        started_at=started_at,
        requested_minutes=minutes,
        elapsed_seconds=elapsed,
        completed=completed,
        latencies=latencies,
        errors=errors,
        health_states=health_states,
        memory_samples=memory_samples,
        restart_count=last_restart_count,
        complete=True,
    )
    return payload


def _checkpoint_result(
    *,
    checkpoint: int,
    elapsed_seconds: float,
    latencies: list[float],
    errors: list[dict[str, Any]],
    health_states: list[str],
    memory_samples: list[float],
    restart_count: int,
) -> dict[str, Any]:
    healthy = bool(health_states) and all(
        state not in {"ERROR", "OFFLINE"} for state in health_states
    )
    passed = (
        elapsed_seconds >= checkpoint * 60
        and healthy
        and len(errors) <= max(3, len(latencies) // 500)
        and restart_count == 0
    )
    return {
        "target_minutes": checkpoint,
        "observed_minutes": round(elapsed_seconds / 60, 3),
        "passed": passed,
        "samples": len(latencies),
        "errors": len(errors),
        "p50_ms": round(statistics.median(latencies), 3) if latencies else 0.0,
        "p95_ms": _percentile(latencies, 95),
        "max_ms": round(max(latencies), 3) if latencies else 0.0,
        "health_states": sorted(set(health_states)),
        "max_memory_percent": round(max(memory_samples), 3) if memory_samples else None,
        "restart_count": restart_count,
    }


def _percentile(values: list[float], percent: float) -> float:
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


def _write_result(
    output: Path,
    *,
    started_at: datetime,
    requested_minutes: int,
    elapsed_seconds: float,
    completed: dict[int, dict[str, Any]],
    latencies: list[float],
    errors: list[dict[str, Any]],
    health_states: list[str],
    memory_samples: list[float],
    restart_count: int,
    complete: bool,
) -> dict[str, Any]:
    payload = {
        "started_at": started_at.isoformat(timespec="seconds"),
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "requested_minutes": requested_minutes,
        "observed_minutes": round(elapsed_seconds / 60, 3),
        "complete": complete,
        "checkpoints": {
            str(key): value for key, value in sorted(completed.items())
        },
        "summary": {
            "samples": len(latencies),
            "errors": len(errors),
            "p50_ms": round(statistics.median(latencies), 3) if latencies else 0.0,
            "p95_ms": _percentile(latencies, 95),
            "p99_ms": _percentile(latencies, 99),
            "max_ms": round(max(latencies), 3) if latencies else 0.0,
            "health_states": sorted(set(health_states)),
            "max_memory_percent": round(max(memory_samples), 3) if memory_samples else None,
            "restart_count": restart_count,
        },
        "error_samples": errors[-20:],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run JARVIS long-duration soak checks.")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--minutes", type=int, default=120)
    parser.add_argument("--checkpoints", default="30,60,120")
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    checkpoints = tuple(
        int(item)
        for item in args.checkpoints.split(",")
        if item.strip()
    )
    result = asyncio.run(
        run_soak(
            base_url=args.url.rstrip("/"),
            minutes=max(1, args.minutes),
            checkpoints=checkpoints,
            interval_seconds=max(0.2, args.interval),
            output=args.output,
        )
    )
    passed = all(
        bool(checkpoint.get("passed"))
        for checkpoint in result["checkpoints"].values()
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
