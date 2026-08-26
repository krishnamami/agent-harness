# ADR-0015: Benchmarks measure the harness, not an agent

- **Status:** Accepted
- **Date:** 2026-08-26

## Context

"Production grade" is a word until there are numbers. But the obvious number —
how long does an agent run take — is the wrong one. It is dominated by model
latency and tool I/O, it changes when someone edits a prompt, and it says
nothing about the thing this repository actually is.

The useful question about a harness is narrower: **what does governance cost on
top of whatever your tools cost?** That has a stable answer, and the only way to
get it honestly is to take the model and the network out.

## Decision

`benchmarks/bench.py` measures the harness with no-op tools and a scripted
planner, so what is timed is the loop, the registry, the policy checks, the
recording and the spans — and nothing else.

**Percentiles, not means.** p50, p95 and p99. A mean hides the tail, and the
tail is the number an operator asks about.

**Warmup discarded.** The first iterations measure import-time laziness and a
cold branch predictor.

**Not a CI gate.** A shared runner's timings are noise, and a performance test
that goes red because another job was compiling something teaches people to
ignore red. Instead, `tests/test_benchmarks.py` runs a tiny one so the script
cannot rot unnoticed — an un-run benchmark breaks silently, and the first person
to find out is the one who needed a number.

**Tracing is measured separately, and off for every other row.** It is a
deployment choice with a real cost, and folding it into the baseline would hide
both facts.

**The exporter is named in the result.** Which matters more than expected — see
below.

## What the numbers said

On the machine recorded in the README, per tool call:

| | |
|---|---|
| Harness overhead, tracing off | ~114µs p50 |
| Harness overhead, tracing on | ~324µs p50 |
| 8 calls at 5ms, serially | 43.5ms |
| 8 calls at 5ms, as one batch | 6.0ms |
| 500 calls across 50 concurrent runs | ~54ms |

Three things worth saying out loud.

**The batch path is worth 7×** on eight calls against a 5ms tool. That is the
whole justification for ADR-0013, measured rather than asserted.

**Tracing roughly triples per-call overhead** — about 200µs. That number is
large next to a no-op tool and negligible next to a real one: for a tool taking
50ms it is 0.4%. Stating it as "3× slower" would be true and misleading, which
is why the table gives the absolute figure.

**Replay costs about the same as the original run.** It should — ADR-0007 says
replay goes through the real executor rather than a simulator, and a replay that
were dramatically faster would be evidence it had stopped doing that.

## Consequences

- The numbers are machine-specific and the README says which machine. Anyone
  quoting them without that is quoting noise.
- They are a floor, not a forecast. "A run costs 4ms" would be a lie about a
  system whose planner spends two seconds thinking.
- `pyproject.toml` gained `"."` on the pytest path so the smoke test can import
  the benchmarks, and `benchmarks` to ruff's source list. CI lints it too.

## Rejected alternatives

**Benchmark with a real model.** Measures the model. The result would move when
a vendor changed a routing policy and would tell nobody anything about this
code.

**Report a mean.** Hides exactly the part anyone cares about.

**Gate CI on the numbers.** Attractive — a performance regression is a real
regression. Rejected because a noisy gate is worse than no gate: it gets
disabled, and then so does the habit of looking. Better to publish the numbers
and re-run them deliberately when the loop changes.

**Report "runs per second".** A single headline figure that means nothing
without saying what a run contains, and it invites comparison with other
projects measuring something else entirely.

**Use `SimpleSpanProcessor` because it is the simplest thing to set up.** This
was the original implementation, and it was wrong for a reason worth recording:
it is not what `app.telemetry` installs, so it measures a configuration nobody
runs. Switching to `BatchSpanProcessor` then produced a *slower* result —
because with an in-memory exporter the batch processor's queue and lock cost
more than a synchronous append to a list. Batching wins when the exporter does
network I/O, which this benchmark deliberately does not do. Both numbers are
therefore honest and neither is the deployment number, which is why the
processor is named in the result row and why `BENCH_SPANS` lets you run it
either way.
