"""Run bounds.

The answer to "what stops an agent running away" has to be structural. These
tests are that answer.
"""

from __future__ import annotations

import pytest

from harness import (
    CostLimitExceededError,
    Principal,
    RiskTier,
    RunContext,
    RunLimits,
    RunOutcome,
    RunResult,
    StepKind,
    StepLimitExceededError,
    StepRecord,
)

P = Principal(id="u1")


def _step(i: int, *, cost: float = 0.0, error: str | None = None) -> StepRecord:
    return StepRecord(index=i, kind=StepKind.TOOL_CALL, summary="s", cost_usd=cost, error=error)


# --------------------------------------------------------------------- limits
def test_limits_reject_nonsense():
    with pytest.raises(ValueError):
        RunLimits(max_steps=0)
    with pytest.raises(ValueError):
        RunLimits(max_cost_usd=0)
    with pytest.raises(ValueError):
        RunLimits(max_consecutive_failures=0)


def test_defaults_are_bounded_not_unlimited():
    """A default of unlimited is the setting nobody revisits."""
    lim = RunLimits()
    assert lim.max_steps > 0
    assert lim.max_cost_usd > 0


# ------------------------------------------------------------------ step ceiling
def test_step_ceiling_stops_the_run():
    ctx = RunContext(P, RunLimits(max_steps=3))
    for i in range(3):
        ctx.before_step()
        ctx.record(_step(i))
    with pytest.raises(StepLimitExceededError):
        ctx.before_step()


# ------------------------------------------------------------------ cost ceiling
def test_cost_is_checked_before_spending_not_after():
    """A run that discovers it is over budget has already spent it."""
    ctx = RunContext(P, RunLimits(max_cost_usd=0.10))
    ctx.before_step(projected_cost_usd=0.06)
    ctx.record(_step(0, cost=0.06))
    with pytest.raises(CostLimitExceededError):
        ctx.before_step(projected_cost_usd=0.06)
    assert ctx.spent_usd == pytest.approx(0.06)


def test_remaining_budget_is_reported():
    ctx = RunContext(P, RunLimits(max_cost_usd=1.00))
    ctx.record(_step(0, cost=0.25))
    assert ctx.remaining_usd == pytest.approx(0.75)


def test_remaining_budget_never_goes_negative():
    ctx = RunContext(P, RunLimits(max_cost_usd=0.10))
    ctx.record(_step(0, cost=0.30))
    assert ctx.remaining_usd == 0.0


# ---------------------------------------------------------------- give-up
def test_consecutive_failures_trigger_give_up():
    ctx = RunContext(P, RunLimits(max_consecutive_failures=3))
    for i in range(2):
        ctx.record(_step(i, error="boom"))
    assert not ctx.should_give_up()
    ctx.record(_step(2, error="boom"))
    assert ctx.should_give_up()


def test_a_success_resets_the_failure_streak():
    """Otherwise a run that recovers still dies from failures it already survived."""
    ctx = RunContext(P, RunLimits(max_consecutive_failures=3))
    ctx.record(_step(0, error="boom"))
    ctx.record(_step(1, error="boom"))
    ctx.record(_step(2))
    assert ctx.consecutive_failures == 0
    assert not ctx.should_give_up()


# ---------------------------------------------------------------- bookkeeping
def test_every_run_has_an_id():
    assert RunContext(P).run_id
    assert RunContext(P).run_id != RunContext(P).run_id


def test_tier_is_carried_on_the_run():
    assert RunContext(P, tier=RiskTier.CRITICAL).tier is RiskTier.CRITICAL


def test_result_exposes_the_tool_calls():
    steps = (
        StepRecord(index=0, kind=StepKind.PLAN, summary="think"),
        _step(1),
        StepRecord(index=2, kind=StepKind.FINISH, summary="done"),
    )
    r = RunResult(run_id="r", outcome=RunOutcome.COMPLETED, steps=steps)
    assert len(r.tool_calls) == 1
    assert r.succeeded


def test_a_bounded_run_is_not_a_successful_run():
    r = RunResult(run_id="r", outcome=RunOutcome.STEP_LIMIT)
    assert not r.succeeded


# ------------------------------------------------------- regression: streaks
def test_a_planning_step_does_not_reset_the_failure_streak():
    """Regression.

    The loop records a PLAN before every TOOL_CALL. The first version reset
    the streak on any successful step, so the counter oscillated between every
    failed call and the give-up ceiling was never reached — a runaway agent
    burned its whole step budget instead of stopping after three failures.
    """
    ctx = RunContext(P, RunLimits(max_consecutive_failures=3))
    for i in range(3):
        ctx.record(StepRecord(index=i * 2, kind=StepKind.PLAN, summary="thinking"))
        ctx.record(_step(i * 2 + 1, error="boom"))
    assert ctx.consecutive_failures == 3
    assert ctx.should_give_up()


def test_a_successful_action_still_resets_the_streak():
    ctx = RunContext(P, RunLimits(max_consecutive_failures=3))
    ctx.record(_step(0, error="boom"))
    ctx.record(_step(1, error="boom"))
    ctx.record(StepRecord(index=2, kind=StepKind.PLAN, summary="thinking"))
    assert ctx.consecutive_failures == 2
    ctx.record(_step(3))
    assert ctx.consecutive_failures == 0
