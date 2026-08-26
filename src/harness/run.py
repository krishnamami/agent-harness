"""Run context, limits, and the record of what happened.

An agent run is bounded by construction. Not monitored and stopped — bounded,
so that "what stops it running away" has a structural answer rather than an
operational one.

Every step is recorded as it happens. That record is what session 4 replays;
capturing it is a design decision made now, because a trace assembled after
the fact from logs is never quite complete enough to reconstruct a decision.
"""

from __future__ import annotations

import time
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


class WallClockExceededError(LimitExceededError):
    """The bound that catches a hang rather than a loop.

    Step and cost ceilings only fire when the loop turns. A run blocked on a
    downstream that never answers never reaches step two, so neither of them
    ever fires -- which is why this one is checked first.
    """

    def __init__(self, limit_seconds: float, elapsed_seconds: float) -> None:
        self.limit_seconds = limit_seconds
        self.elapsed_seconds = elapsed_seconds
        super().__init__(f"run exceeded {limit_seconds:.0f}s (elapsed {elapsed_seconds:.1f}s)")


@dataclass(frozen=True)
class RunLimits:
    """The bounds a run cannot exceed.

    Every ceiling is required rather than optional. A default of "unlimited"
    is the setting nobody revisits, and the first time it matters is the
    invoice.

    Four bounds, because they catch four different runaways: a loop that will
    not terminate (`max_steps`), a run that is expensive rather than long
    (`max_cost_usd`), an agent retrying something that will never work
    (`max_consecutive_failures`), and a call that simply never returns
    (`max_wall_clock_seconds`). The last is the one most harnesses omit, and
    it is the only one that catches a hang.
    """

    max_steps: int = 25
    max_cost_usd: float = 1.00
    max_consecutive_failures: int = 3
    max_wall_clock_seconds: float = 300.0
    default_tool_timeout_seconds: float = 30.0
    max_delegation_depth: int = 3
    max_parallel_calls: int = 8

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if self.max_cost_usd <= 0:
            raise ValueError("max_cost_usd must be positive")
        if self.max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be at least 1")
        if self.max_wall_clock_seconds <= 0:
            raise ValueError("max_wall_clock_seconds must be positive")
        if self.default_tool_timeout_seconds <= 0:
            raise ValueError("default_tool_timeout_seconds must be positive")
        if self.max_delegation_depth < 0:
            raise ValueError("max_delegation_depth cannot be negative")
        if self.max_parallel_calls < 1:
            raise ValueError("max_parallel_calls must be at least 1")
        # Deliberately no cross-check that the per-call timeout fits inside the
        # run ceiling. `RunContext.timeout_for` clamps every call to whatever
        # the run has left, so a generous per-call default is already harmless
        # -- and rejecting it here would make the natural
        # `RunLimits(max_wall_clock_seconds=10)` raise on the default.


class StepKind(StrEnum):
    PLAN = "plan"
    APPROVAL = "approval"
    TOOL_CALL = "tool_call"
    DELEGATION = "delegation"
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
        depth: int = 0,
    ) -> None:
        self.run_id = run_id or str(uuid.uuid4())
        self.principal = principal
        self.limits = limits or RunLimits()
        self.tier = tier
        self.metadata = metadata or {}
        self.depth = depth
        # Results of runs delegated from this one, in the order they were
        # started. The parent's own `steps` stay a record of the parent's own
        # actions -- a delegation appears there as one step, not as the child's
        # twenty, or the failure streak would count a single failed sub-run as
        # twenty failures.
        self.sub_runs: list[SubRun] = []
        self.steps: list[StepRecord] = []
        self.spent_usd = 0.0
        self._consecutive_failures = 0
        # Monotonic, not wall time: a run must not be extended or truncated by
        # an NTP correction landing mid-flight.
        self._started_monotonic = time.monotonic()

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limits.max_cost_usd - self.spent_usd)

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started_monotonic

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.limits.max_wall_clock_seconds - self.elapsed_seconds)

    def timeout_for(self, tool_timeout_seconds: float | None = None) -> float:
        """How long one call may take, given how long the run has left.

        Clamped to the remaining wall clock. Without the clamp a tool with a
        generous per-call timeout could carry the run well past its own
        ceiling: the bound would hold for the call and not for the run, which
        is the same as not holding.
        """
        limit = tool_timeout_seconds or self.limits.default_tool_timeout_seconds
        return min(limit, self.remaining_seconds)

    def before_step(self, projected_cost_usd: float = 0.0) -> None:
        """Raise if taking another step would breach a bound."""
        # Time is checked first because it is the bound the others cannot
        # substitute for. A run stuck on one call never turns the loop again,
        # so no step-based or cost-based ceiling will ever fire.
        if self.remaining_seconds <= 0:
            raise WallClockExceededError(self.limits.max_wall_clock_seconds, self.elapsed_seconds)
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
    TIME_LIMIT = "time_limit"
    DEPTH_LIMIT = "depth_limit"
    GAVE_UP = "gave_up"
    DENIED = "denied"
    # A human said no. Distinct from DENIED, which is policy refusing a call
    # the agent may route around; a refusal is terminal, because an agent that
    # rephrases until someone approves is worse than no gate at all.
    NOT_APPROVED = "not_approved"
    FAILED = "failed"


@dataclass(frozen=True)
class SubRun:
    """A delegated run, kept whole.

    The parent holds the child's *context* as well as its result, because a
    result alone cannot be turned into a trace: the limits that were in force,
    the principal it ran as and the tier it ran at all live on the context, and
    those are most of what makes a trace worth keeping.

    Keeping only the result was the original design, and it made every
    delegated run unexplainable -- which contradicted ADR-0007 from inside
    ADR-0011. See ADR-0016.
    """

    context: RunContext
    result: RunResult

    @property
    def goal(self) -> str:
        return str(self.context.metadata.get("sub_goal", ""))


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
