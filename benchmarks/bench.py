"""What the harness costs.

Every number here measures **the harness**, not an agent. The tools are no-ops
and the planner is scripted, so what is being timed is the loop, the registry,
the policy checks, the recording and the spans -- and nothing else.

That is the point. A real agent's latency is dominated by model calls and tool
I/O, both of which are somebody else's milliseconds. The useful question about
a harness is what governance costs on top of that, and the only way to answer
it honestly is to take the model and the network out.

Read these as a floor, not a forecast. "A run costs 4ms" would be a lie about a
system whose planner spends two seconds thinking; "the harness adds 40µs per
step to whatever your tools cost" is a claim that survives contact with one.

Run:  uv run python -m benchmarks.bench
"""

from __future__ import annotations

import asyncio
import os
import platform
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from harness import (
    CallTool,
    CallTools,
    Finish,
    Principal,
    RunContext,
    RunLimits,
    ScriptedPlanner,
    ToolRegistry,
    ToolSpec,
    delegate,
    record_trace,
    replay,
    run_agent,
)

OBJ: dict[str, object] = {"type": "object", "properties": {}}
P = Principal(id="bench", roles=frozenset({"analyst"}))

# Generous, so no benchmark is measuring a ceiling being hit.
LIMITS = RunLimits(max_steps=2000, max_cost_usd=1e6, max_wall_clock_seconds=3600)


async def _noop(arguments: dict[str, object]) -> dict[str, bool]:
    return {"ok": True}


def _registry(fn: Callable[..., Awaitable[object]] = _noop) -> ToolRegistry:
    r = ToolRegistry()
    r.register(ToolSpec(name="t", description="t", parameters=OBJ), fn)
    return r


@dataclass(frozen=True)
class Result:
    name: str
    unit: str
    p50: float
    p95: float
    p99: float
    note: str = ""

    def row(self) -> str:
        return (
            f"| {self.name} | {self.p50:,.1f} | {self.p95:,.1f} | "
            f"{self.p99:,.1f} | {self.unit} | {self.note} |"
        )


async def measure(
    name: str,
    body: Callable[[], Awaitable[None]],
    *,
    reps: int,
    warmup: int = 20,
    unit: str = "µs",
    divisor: float = 1.0,
    note: str = "",
) -> Result:
    """Time `body` `reps` times and report percentiles.

    Percentiles rather than a mean: a mean hides the tail, and the tail is the
    number an SRE asks about. Warmup runs are discarded -- the first few
    iterations are measuring import-time laziness and a cold branch predictor,
    not the code.
    """
    for _ in range(warmup):
        await body()

    samples: list[float] = []
    for _ in range(reps):
        started = time.perf_counter()
        await body()
        samples.append((time.perf_counter() - started) * 1_000_000 / divisor)

    samples.sort()
    return Result(
        name=name,
        unit=unit,
        p50=statistics.median(samples),
        p95=samples[int(len(samples) * 0.95) - 1],
        p99=samples[int(len(samples) * 0.99) - 1],
        note=note,
    )


# ---------------------------------------------------------------- the loop
async def bench_step_overhead(steps: int = 25, reps: int = 300) -> Result:
    """Per-step cost of the loop with nothing real in it."""
    registry = _registry()
    # Each turn is a PLAN + a TOOL_CALL, so a run of `steps` calls records
    # 2*steps + 1 steps. The divisor is tool calls, which is what a reader
    # means by "a step".
    script = [CallTool(tool="t", arguments={}) for _ in range(steps)]

    async def body() -> None:
        planner = ScriptedPlanner(*script, Finish(output=None))
        await run_agent("bench", planner, registry, RunContext(P, LIMITS))

    return await measure(
        "Loop overhead, per tool call",
        body,
        reps=reps,
        divisor=steps,
        note=f"{steps} calls/run, no-op tool",
    )


async def bench_empty_run(reps: int = 500) -> Result:
    """Fixed cost of starting and finishing a run that does nothing."""
    registry = _registry()

    async def body() -> None:
        await run_agent(
            "bench", ScriptedPlanner(Finish(output=None)), registry, RunContext(P, LIMITS)
        )

    return await measure("Run setup and teardown", body, reps=reps, note="plan + finish only")


# ------------------------------------------------------------ concurrency
async def bench_batch(width: int = 8, hold_ms: float = 5.0, reps: int = 60) -> list[Result]:
    """One batch of `width` calls against `width` sequential turns.

    The tool holds for a fixed time, standing in for a downstream that takes
    milliseconds rather than microseconds -- which is every real tool.
    """
    hold = hold_ms / 1000

    async def slow(arguments: dict[str, object]) -> dict[str, bool]:
        await asyncio.sleep(hold)
        return {"ok": True}

    registry = _registry(slow)
    serial_script = [CallTool(tool="t", arguments={}) for _ in range(width)]
    batch = CallTools(calls=tuple(CallTool(tool="t", arguments={}) for _ in range(width)))

    async def serial() -> None:
        planner = ScriptedPlanner(*serial_script, Finish(output=None))
        await run_agent("bench", planner, registry, RunContext(P, LIMITS))

    async def parallel() -> None:
        planner = ScriptedPlanner(batch, Finish(output=None))
        await run_agent("bench", planner, registry, RunContext(P, LIMITS))

    return [
        await measure(
            f"{width} calls, one at a time",
            serial,
            reps=reps,
            warmup=5,
            unit="ms",
            divisor=1000,
            note=f"{hold_ms:.0f}ms tool",
        ),
        await measure(
            f"{width} calls, one batch",
            parallel,
            reps=reps,
            warmup=5,
            unit="ms",
            divisor=1000,
            note=f"{hold_ms:.0f}ms tool",
        ),
    ]


async def bench_concurrent_runs(runs: int = 50, steps: int = 10, reps: int = 40) -> Result:
    """Many runs in flight at once, in one process."""
    registry = _registry()
    script = [CallTool(tool="t", arguments={}) for _ in range(steps)]

    async def body() -> None:
        await asyncio.gather(
            *(
                run_agent(
                    "bench",
                    ScriptedPlanner(*script, Finish(output=None)),
                    registry,
                    RunContext(P, LIMITS),
                )
                for _ in range(runs)
            )
        )

    return await measure(
        f"{runs} concurrent runs",
        body,
        reps=reps,
        warmup=5,
        unit="ms",
        divisor=1000,
        note=f"{steps} calls each, {runs * steps} calls total",
    )


# ------------------------------------------------------------- delegation
async def bench_delegation(reps: int = 300) -> Result:
    """What a delegation costs over running the same work inline."""
    registry = _registry()

    async def body() -> None:
        parent = RunContext(P, LIMITS)
        await delegate(
            parent,
            "sub",
            ScriptedPlanner(CallTool(tool="t", arguments={}), Finish(output=None)),
            registry,
        )

    return await measure(
        "Delegation, one child", body, reps=reps, note="narrowing + draw + child run"
    )


# ------------------------------------------------------------- the record
async def bench_trace(steps: int = 25, reps: int = 200) -> list[Result]:
    """Recording, serialising and replaying a run.

    On the hot path of every run that keeps a trace, which in a regulated
    deployment is all of them.
    """
    registry = _registry()
    planner = ScriptedPlanner(
        *[CallTool(tool="t", arguments={}) for _ in range(steps)], Finish(output=None)
    )
    ctx = RunContext(P, LIMITS)
    result = await run_agent("bench", planner, registry, ctx)
    trace = record_trace("bench", ctx, result, registry)

    async def record() -> None:
        record_trace("bench", ctx, result, registry)

    async def serialise() -> None:
        trace.to_dict()

    async def do_replay() -> None:
        await replay(trace)

    return [
        await measure("Record a trace", record, reps=reps, note=f"{steps}-call run"),
        await measure("Serialise a trace", serialise, reps=reps, note=f"{steps}-call run"),
        await measure(
            "Replay a trace",
            do_replay,
            reps=max(reps // 4, 20),
            note=f"{steps}-call run, through the real executor",
        ),
    ]


# ------------------------------------------------------------------ spans
async def bench_spans(steps: int = 25, reps: int = 200) -> list[Result]:
    """The cost of telemetry, measured by turning it on.

    Order matters and cannot be reversed: OpenTelemetry's global provider can
    be set once per process. So the no-provider case is measured first, while
    `get_tracer` is still resolving to a no-op, and the processor under test is
    chosen by `BENCH_SPANS` -- run the file twice to compare two of them.

    The default is `batch`, because `BatchSpanProcessor` is what `app.telemetry`
    installs and therefore what a deployment actually pays. `simple` exports
    synchronously on every span end; it is the obvious thing to reach for in a
    benchmark and it measures a configuration nobody runs.
    """
    before = await bench_step_overhead(steps=steps, reps=reps)

    from opentelemetry import trace as otel
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    mode = os.environ.get("BENCH_SPANS", "batch")
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter) if mode == "simple" else BatchSpanProcessor(exporter)
    provider = TracerProvider()
    provider.add_span_processor(processor)
    otel.set_tracer_provider(provider)

    after = await bench_step_overhead(steps=steps, reps=reps)

    return [
        Result(
            "Per call, tracing off",
            before.unit,
            before.p50,
            before.p95,
            before.p99,
            "no provider installed",
        ),
        Result(
            f"Per call, tracing on ({mode})",
            after.unit,
            after.p50,
            after.p95,
            after.p99,
            f"{type(processor).__name__}",
        ),
    ]


async def main() -> None:
    results: list[Result] = []
    results.append(await bench_empty_run())
    results.append(await bench_step_overhead())
    results.append(await bench_delegation())
    results.extend(await bench_trace())
    results.extend(await bench_batch())
    results.append(await bench_concurrent_runs())
    # Last, because it installs a global tracer provider that every later
    # measurement would then be paying for.
    results.extend(await bench_spans())

    print(f"\nPython {platform.python_version()} on {platform.platform()}")
    print("Tracing is off for every row except the last two.")
    print(f"{platform.processor() or 'unknown cpu'}\n")
    print("| Measurement | p50 | p95 | p99 | unit | notes |")
    print("|---|---:|---:|---:|---|---|")
    for r in results:
        print(r.row())
    print()


if __name__ == "__main__":
    asyncio.run(main())
