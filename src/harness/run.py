"""Run context, limits, and the record of what happened.

An agent run is bounded by construction. Not monitored and stopped — bounded,
so that "what stops it running away" has a structural answer rather than an
operational one.

Every step is recorded as it happens. That record is what session 4 replays;
capturing it is a design decision made now, because a trace assembled after
the fact from logs is never quite complete enough to reconstruct a decision.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from harness.policy import Principal, RiskTier


class LimitExceededError(Exception):
    """Base for every bound the harness enforces."""


class StepLimitExceededError(LimitExceededError):
    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"run exceeded {limit} steps")


class CostLimitExceededError(LimitExceededError):
    def __init__(self, limit_usd: float, spent_usd: float) -> None:
        self.limit_usd = limit_usd
        self.spent_usd = spent_usd
        super().__init__(f"run exceeded ${limit_usd:.2f} (spent ${spent_usd:.4f})")


@dataclass(frozen=True)
class RunLimits:
    """The bounds a run cannot exceed.

    Both ceilings are required rather than optional. A default of "unlimited"
    is the setting nobody revisits, and the first time it matters is the
    invoice.
    """

    max_steps: int = 25
    max_cost_usd: float = 1.00
    max_consecutive_failures: int = 3

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if self.max_cost_usd <= 0:
            raise ValueError("max_cost_usd must be positive")
        if self.max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be at least 1")


class StepKind(StrEnum):
    PLAN = "plan"
    APPROVAL = "approval"
    TOOL_CALL = "tool_call"
    OBSERVATION = "observation"
    CORRECTION = "correction"
    FINISH = "finish"


@dataclass(frozen=True)
class StepRecord:
    """One thing that happened, in enough detail to replay it."""

    index: int
    kind: StepKind
    summary: str
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    result: Any = None
    error: str | None = None
    cost_usd: float = 0.0
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.error is not None


class RunContext:
    """Mutable state for one run, and the thing that enforces the bounds.

    Bounds are checked on the way in (`before_step`) rather than after. A run
    that discovers it has exceeded its budget has already spent it.
    """

    def __init__(
        self,
        principal: Principal,
        limits: RunLimits | None = None,
        tier: RiskTier = RiskTier.ROUTINE,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.run_id = run_id or str(uuid.uuid4())
        self.principal = principal
        self.limits = limits or RunLimits()
        self.tier = tier
        self.metadata = metadata or {}
        self.steps: list[StepRecord] = []
        self.spent_usd = 0.0
        self._consecutive_failures = 0

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limits.max_cost_usd - self.spent_usd)

    def before_step(self, projected_cost_usd: float = 0.0) -> None:
        """Raise if taking another step would breach a bound."""
        if self.step_count >= self.limits.max_steps:
            raise StepLimitExceededError(self.limits.max_steps)
        projected = self.spent_usd + projected_cost_usd
        if projected > self.limits.max_cost_usd:
            raise CostLimitExceededError(self.limits.max_cost_usd, projected)

    def record(self, step: StepRecord) -> None:
        self.steps.append(step)
        self.spent_usd += step.cost_usd

        # Only *actions* move the failure streak. Planning is neutral.
        #
        # This is not a detail. Each loop iteration records a PLAN step and
        # then a TOOL_CALL step, so if a successful plan reset the streak the
        # counter would oscillate 0-1-0-1 between every failed call and the
        # give-up ceiling would never be reached. A runaway agent would burn
        # its entire step budget instead of stopping after three failures.
        # We shipped that and the test below caught it.
        if step.kind in (StepKind.PLAN, StepKind.APPROVAL):
            return

        if step.failed:
            self._consecutive_failures += 1
        else:
            self._consecutive_failures = 0

    def should_give_up(self) -> bool:
        """The explicit give-up condition.

        Self-correction without one is not self-correction; it is failing
        slowly and expensively. A run that has failed this many times in a row
        is not converging, and further attempts spend budget to learn nothing.
        """
        return self._consecutive_failures >= self.limits.max_consecutive_failures


class RunOutcome(StrEnum):
    COMPLETED = "completed"
    STEP_LIMIT = "step_limit"
    COST_LIMIT = "cost_limit"
    GAVE_UP = "gave_up"
    DENIED = "denied"
    # A human said no. Distinct from DENIED, which is policy refusing a call
    # the agent may route around; a refusal is terminal, because an agent that
    # rephrases until someone approves is worse than no gate at all.
    NOT_APPROVED = "not_approved"
    FAILED = "failed"


@dataclass(frozen=True)
class RunResult:
    """What a run produced, and everything needed to explain it."""

    run_id: str
    outcome: RunOutcome
    output: Any = None
    steps: tuple[StepRecord, ...] = ()
    cost_usd: float = 0.0
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome is RunOutcome.COMPLETED

    @property
    def tool_calls(self) -> tuple[StepRecord, ...]:
        return tuple(s for s in self.steps if s.kind is StepKind.TOOL_CALL)
