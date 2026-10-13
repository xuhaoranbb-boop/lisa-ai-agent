# JARVIS Benchmark

Run:

```powershell
python -m uv run python scripts/run_jarvis_benchmark.py
```

Results are written to `benchmark/results/`; the aggregate score is written to
`JARVIS_SCORE.md`.

The numbered directories map to the phase 6 benchmark categories:
voice, memory, context, planner, tools, desktop, browser, vision, workflow,
recovery, security, and long-running stability.

Long-duration soak tests are reported as BLOCKED unless they are actually run
for their full configured duration. Short-duration probes are not presented as
substitutes for a 30, 60, or 120 minute soak.
