"""The benchmarks are code too.

They are not run as a gate -- a shared CI runner's timings are noise, and a
performance test that fails because another job was compiling something teaches
people to ignore red. But an un-run benchmark rots silently, and the first time
anyone notices is when they need a number and the script no longer imports.

So: run a tiny one, and check the measurement machinery itself is honest.
"""

from __future__ import annotations

from benchmarks.bench import bench_empty_run, measure


async def test_a_benchmark_still_runs():
    result = await bench_empty_run(reps=3)

    assert result.p50 > 0
    assert result.unit == "µs"
    assert "|" in result.row()


async def test_percentiles_are_ordered():
    # Cheap, and it catches the off-by-one in the index arithmetic that every
    # hand-rolled percentile eventually has.
    async def body() -> None:
        return None

    result = await measure("noop", body, reps=100, warmup=2)

    assert result.p50 <= result.p95 <= result.p99
