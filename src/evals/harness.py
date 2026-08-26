"""The runner.

Executes every case against a target, scores each output with every evaluator,
and records latency alongside quality. Cost and latency are first-class here,
not an afterthought: a system that is 3% more accurate and four times more
expensive is usually a worse system, and an eval report that only shows quality
cannot tell you that.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from evals.dataset import Dataset, DatasetInfo
from evals.protocol import EvalCase, Evaluator, Score

# The thing under test: takes a case, returns whatever it returns.
Target = Callable[[EvalCase], Awaitable[Any]]


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    output: Any
    scores: tuple[Score, ...]
    latency_ms: float
    tags: tuple[str, ...] = ()
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None or any(not s.passed for s in self.scores)


@dataclass(frozen=True)
class Aggregate:
    metric: str
    n: int
    mean: float
    median: float
    minimum: float
    pass_rate: float


@dataclass(frozen=True)
class EvalRun:
    dataset: DatasetInfo
    results: tuple[CaseResult, ...]
    duration_s: float
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> tuple[CaseResult, ...]:
        return tuple(r for r in self.results if r.error is not None)

    def latency_p95_ms(self) -> float:
        values = sorted(r.latency_ms for r in self.results)
        if not values:
            return 0.0
        # Nearest-rank p95. On small golden sets this is the honest one:
        # interpolation invents a number that no case actually produced.
        index = max(0, min(len(values) - 1, round(0.95 * len(values)) - 1))
        return values[index]

    def aggregate(self) -> dict[str, Aggregate]:
        by_metric: dict[str, list[Score]] = {}
        for result in self.results:
            for score in result.scores:
                by_metric.setdefault(score.name, []).append(score)

        out: dict[str, Aggregate] = {}
        for metric, scores in by_metric.items():
            values = [s.value for s in scores]
            out[metric] = Aggregate(
                metric=metric,
                n=len(scores),
                mean=statistics.fmean(values),
                median=statistics.median(values),
                minimum=min(values),
                pass_rate=sum(1 for s in scores if s.passed) / len(scores),
            )
        return out


async def _run_one(case: EvalCase, target: Target, evaluators: Sequence[Evaluator]) -> CaseResult:
    start = time.perf_counter()
    try:
        output = await target(case)
    except Exception as exc:
        # A target that blows up is a result, not a crashed run. Losing the
        # other 199 cases because one raised is the worst possible trade.
        return CaseResult(
            case_id=case.id,
            output=None,
            scores=(),
            latency_ms=(time.perf_counter() - start) * 1000,
            tags=case.tags,
            error=f"{type(exc).__name__}: {exc}",
        )

    latency_ms = (time.perf_counter() - start) * 1000

    scores: list[Score] = []
    for evaluator in evaluators:
        try:
            scores.append(await evaluator.score(case, output))
        except Exception as exc:
            # An evaluator that raises is a bug in the evaluator, and it must
            # not be able to make a case silently disappear from the report.
            scores.append(
                Score(
                    name=evaluator.name,
                    value=0.0,
                    passed=False,
                    detail=f"evaluator raised {type(exc).__name__}: {exc}",
                )
            )

    return CaseResult(
        case_id=case.id,
        output=output,
        scores=tuple(scores),
        latency_ms=latency_ms,
        tags=case.tags,
    )


async def run_eval(
    dataset: Dataset,
    target: Target,
    evaluators: Sequence[Evaluator],
    concurrency: int = 8,
    metadata: dict[str, Any] | None = None,
) -> EvalRun:
    """Run every case, bounded by `concurrency`.

    Bounded rather than unbounded because the target is usually a rate-limited
    API. Firing 200 cases at once produces a report full of 429s that says
    nothing about quality.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")

    semaphore = asyncio.Semaphore(concurrency)

    async def guarded(case: EvalCase) -> CaseResult:
        async with semaphore:
            return await _run_one(case, target, evaluators)

    start = time.perf_counter()
    results = await asyncio.gather(*(guarded(c) for c in dataset.cases))
    duration = time.perf_counter() - start

    return EvalRun(
        dataset=dataset.info,
        results=tuple(results),
        duration_s=duration,
        metadata=metadata or {},
    )
