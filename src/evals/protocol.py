"""Evaluation contracts.

Three types, and the shape of `Score` is the load-bearing one.

A score carries **both** a continuous value and a pass/fail. That is deliberate:
a mean of 0.82 tells you the average is fine and hides that one case in six is
catastrophic. Reporting a mean without a pass rate is how a system ships with a
known failure mode nobody noticed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class EvalCase:
    """One item of a golden set.

    `expected` is optional. Reference-free evaluation is normal for generative
    systems -- a groundedness check compares the answer to the retrieved
    context, not to a gold answer -- so a case with no expected value is a
    first-class citizen, not a missing field.
    """

    id: str
    inputs: dict[str, Any]
    expected: dict[str, Any] | None = None
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Score:
    """The result of one evaluator on one case.

    `value` is a probability-like number in [0, 1] so that scores from
    different evaluators can be aggregated and compared without a lookup table
    of what each scale means.
    """

    name: str
    value: float
    passed: bool
    detail: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.value <= 1.0:
            raise ValueError(f"score {self.name}={self.value} is outside [0, 1]")


@runtime_checkable
class Evaluator(Protocol):
    """Scores one case's output.

    An evaluator must not raise on bad output -- a model returning nonsense is
    the thing being measured, not an error condition. Return a failing score.
    """

    name: str

    async def score(self, case: EvalCase, output: Any) -> Score: ...
