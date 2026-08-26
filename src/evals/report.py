"""Reporting.

Two renderers. The text one is read by a human scanning a CI log; the JSON one
is read by whatever stores run history. Both come from the same EvalRun, so
they cannot disagree.
"""

from __future__ import annotations

import json
from typing import Any

from evals.harness import EvalRun
from evals.thresholds import GateResult


def to_dict(run: EvalRun, gate: GateResult | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "dataset": {
            "name": run.dataset.name,
            "sha256": run.dataset.sha256,
            "case_count": run.dataset.case_count,
        },
        "duration_s": round(run.duration_s, 3),
        "latency_p95_ms": round(run.latency_p95_ms(), 2),
        "error_count": len(run.errors),
        "metrics": {
            name: {
                "n": agg.n,
                "mean": round(agg.mean, 4),
                "median": round(agg.median, 4),
                "min": round(agg.minimum, 4),
                "pass_rate": round(agg.pass_rate, 4),
            }
            for name, agg in sorted(run.aggregate().items())
        },
        "metadata": run.metadata,
    }
    if gate is not None:
        payload["gate"] = {
            "passed": gate.passed,
            "violations": [
                {
                    "metric": v.metric,
                    "check": v.check,
                    "expected": v.expected,
                    "actual": v.actual,
                }
                for v in gate.violations
            ],
            "notes": list(gate.notes),
        }
    return payload


def to_json(run: EvalRun, gate: GateResult | None = None) -> str:
    return json.dumps(to_dict(run, gate), indent=2, default=str)


def to_text(run: EvalRun, gate: GateResult | None = None) -> str:
    lines: list[str] = []
    lines.append(f"dataset   {run.dataset}")
    lines.append(
        f"run       {len(run.results)} cases in {run.duration_s:.2f}s, "
        f"p95 {run.latency_p95_ms():.0f}ms, {len(run.errors)} errors"
    )
    lines.append("")

    aggregates = run.aggregate()
    if aggregates:
        lines.append(f"{'metric':<28}{'mean':>8}{'median':>9}{'min':>8}{'pass':>8}")
        lines.append("-" * 61)
        for name, agg in sorted(aggregates.items()):
            lines.append(
                f"{name:<28}{agg.mean:>8.3f}{agg.median:>9.3f}"
                f"{agg.minimum:>8.3f}{agg.pass_rate:>7.0%}"
            )
        lines.append("")

    failures = [r for r in run.results if r.failed]
    if failures:
        lines.append(f"failing cases ({len(failures)}):")
        # Truncated deliberately: a CI log with 200 failing cases in it is not
        # read. The JSON report carries all of them.
        for result in failures[:10]:
            reason = result.error or ", ".join(
                f"{s.name}={s.value:.2f}" for s in result.scores if not s.passed
            )
            lines.append(f"  {result.case_id:<24} {reason}")
        if len(failures) > 10:
            lines.append(f"  ... and {len(failures) - 10} more (see the JSON report)")
        lines.append("")

    if gate is not None:
        for note in gate.notes:
            lines.append(f"note: {note}")
        lines.append(gate.summary())

    return "\n".join(lines)
