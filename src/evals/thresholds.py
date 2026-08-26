"""The Production Quality Bar.

Thresholds are configuration, not code, for the same reason the coverage floor
lives in pyproject.toml (ADR-0007): a number defined inside the pipeline drifts
from the number developers see locally.

Two kinds of check, and both are needed:

- **Absolute** -- "groundedness must average at least 0.85". Catches a system
  that was never good enough.
- **Regression** -- "no more than 2 points below the last accepted run".
  Catches the slow decay that an absolute floor never notices, because a system
  sliding from 0.95 to 0.86 is in trouble and still passing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from evals.harness import EvalRun


@dataclass(frozen=True)
class Threshold:
    """A condition a run must satisfy.

    `min_pass_rate` alongside `min_mean` is not redundant. A mean of 0.9 with a
    pass rate of 0.7 means three cases in ten fail badly while the average
    looks healthy -- and for a regulated use case the failing three are the
    only ones that matter.
    """

    metric: str
    min_mean: float | None = None
    min_pass_rate: float | None = None
    min_score: float | None = None
    max_regression: float | None = None


@dataclass(frozen=True)
class Violation:
    metric: str
    check: str
    expected: float
    actual: float

    def __str__(self) -> str:
        return f"{self.metric}.{self.check}: {self.actual:.3f} (required {self.expected:.3f})"


@dataclass(frozen=True)
class GateResult:
    passed: bool
    violations: tuple[Violation, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def summary(self) -> str:
        if self.passed:
            return "PASS"
        return "FAIL: " + "; ".join(str(v) for v in self.violations)


@dataclass(frozen=True)
class Gate:
    thresholds: tuple[Threshold, ...]
    max_p95_latency_ms: float | None = None
    max_error_rate: float = 0.0

    def evaluate(self, run: EvalRun, baseline: EvalRun | None = None) -> GateResult:
        violations: list[Violation] = []
        notes: list[str] = []

        if baseline is not None and baseline.dataset.sha256 != run.dataset.sha256:
            # Comparing across datasets is meaningless, so we refuse rather
            # than silently produce a confident wrong answer.
            notes.append(
                f"baseline skipped: dataset changed "
                f"({baseline.dataset.short_sha} -> {run.dataset.short_sha})"
            )
            baseline = None

        error_rate = len(run.errors) / len(run.results) if run.results else 0.0
        if error_rate > self.max_error_rate:
            violations.append(Violation("run", "error_rate", self.max_error_rate, error_rate))

        if self.max_p95_latency_ms is not None:
            p95 = run.latency_p95_ms()
            if p95 > self.max_p95_latency_ms:
                violations.append(Violation("run", "p95_latency_ms", self.max_p95_latency_ms, p95))

        aggregates = run.aggregate()
        baseline_aggregates = baseline.aggregate() if baseline else {}

        for threshold in self.thresholds:
            agg = aggregates.get(threshold.metric)
            if agg is None:
                violations.append(Violation(threshold.metric, "present", 1.0, 0.0))
                continue

            if threshold.min_mean is not None and agg.mean < threshold.min_mean:
                violations.append(Violation(threshold.metric, "mean", threshold.min_mean, agg.mean))
            if threshold.min_pass_rate is not None and agg.pass_rate < threshold.min_pass_rate:
                violations.append(
                    Violation(threshold.metric, "pass_rate", threshold.min_pass_rate, agg.pass_rate)
                )
            if threshold.min_score is not None and agg.minimum < threshold.min_score:
                violations.append(
                    Violation(threshold.metric, "min_score", threshold.min_score, agg.minimum)
                )
            if threshold.max_regression is not None:
                previous = baseline_aggregates.get(threshold.metric)
                if previous is not None:
                    drop = previous.mean - agg.mean
                    if drop > threshold.max_regression:
                        violations.append(
                            Violation(
                                threshold.metric,
                                "regression",
                                threshold.max_regression,
                                drop,
                            )
                        )

        return GateResult(passed=not violations, violations=tuple(violations), notes=tuple(notes))
