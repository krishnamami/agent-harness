"""Time bounds.

The bound that catches a hang rather than a loop.

Step and cost ceilings only fire when the loop turns, and a run blocked on a
downstream that never answers never turns it again. Before these existed,
"bounded by construction" was true of runaway loops and quietly false of the
much more common failure: one call that simply does not come back.
"""

from __future__ import annotations

import asyncio

import pytest

from harness import (
    CallTool,
    Finish,
    Principal,
    RunContext,
    RunLimits,
    RunOutcome,
    RunTrace,
    ScriptedPlanner,
    StepKind,
    ToolRegistry,
    ToolSpec,
    WallClockExceededError,
    record_trace,
    run_agent,
)

OBJ = {"type": "object", "properties": {"q": {"type": "string"}}}
P = Principal(id="u1", roles=frozenset({"analyst"}))


async def _ok(arguments):
    return {"ok": True}


async def _hang(arguments):
    await asyncio.sleep(60)
    return {"never": True}  # pragma: no cover


def _registry(spec: ToolSpec, fn) -> ToolRegistry:
    r = ToolRegistry()
    r.register(spec, fn)
    return r


def _spend(ctx: RunContext, seconds: float) -> None:
    """Age a run without waiting for it.

    A test asserting on a five-minute ceiling should not take five minutes,
    and a test that sleeps its way to an assertion is one that will eventually
    fail on a loaded CI runner for reasons unrelated to the code.
    """
    ctx._started_monotonic -= seconds


# --------------------------------------------------------------- configuration
def test_limits_reject_a_non_positive_wall_clock():
    with pytest.raises(ValueError, match="max_wall_clock_seconds"):
        RunLimits(max_wall_clock_seconds=0)


def test_limits_reject_a_non_positive_tool_timeout():
    with pytest.raises(ValueError, match="default_tool_timeout_seconds"):
        RunLimits(default_tool_timeout_seconds=-1)


def test_a_short_run_does_not_have_to_restate_the_tool_timeout():
    # The clamp handles it. Requiring both to be set together would make the
    # obvious way to ask for a ten-second run raise on a default nobody chose.
    ctx = RunContext(P, RunLimits(max_wall_clock_seconds=10))
    assert ctx.timeout_for(None) <= 10


def test_a_tool_spec_rejects_a_non_positive_timeout():
    with pytest.raises(ValueError, match="timeout_seconds"):
        ToolSpec(name="t", description="t", parameters=OBJ, timeout_seconds=0)


# ------------------------------------------------------------------- the clock
def test_a_fresh_run_has_its_whole_budget():
    ctx = RunContext(P, RunLimits(max_wall_clock_seconds=100))
    assert ctx.elapsed_seconds >= 0
    assert 99 < ctx.remaining_seconds <= 100


def test_remaining_time_never_goes_negative():
    ctx = RunContext(P, RunLimits(max_wall_clock_seconds=10))
    _spend(ctx, 60)
    assert ctx.remaining_seconds == 0


def test_before_step_raises_once_the_wall_clock_is_spent():
    ctx = RunContext(P, RunLimits(max_wall_clock_seconds=10))
    _spend(ctx, 11)
    with pytest.raises(WallClockExceededError) as exc:
        ctx.before_step()
    assert exc.value.limit_seconds == 10


def test_time_is_checked_before_the_other_ceilings():
    # A run both out of time and out of steps reports the time, because time is
    # what stopped it -- it ran out first by definition. An operator tuning
    # max_steps upward in response would be turning the wrong dial.
    ctx = RunContext(P, RunLimits(max_steps=1, max_wall_clock_seconds=10))
    _spend(ctx, 11)
    with pytest.raises(WallClockExceededError):
        ctx.before_step()


# ----------------------------------------------------------------- the clamp
def test_timeout_for_falls_back_to_the_run_default():
    ctx = RunContext(P, RunLimits(default_tool_timeout_seconds=30))
    assert ctx.timeout_for(None) == 30


def test_timeout_for_prefers_the_tools_own_timeout():
    ctx = RunContext(P, RunLimits(default_tool_timeout_seconds=30))
    assert ctx.timeout_for(5) == 5


def test_timeout_for_is_clamped_to_what_the_run_has_left():
    # The load-bearing case. A tool allowed 30 seconds, asked with 2 seconds of
    # run left, gets 2 -- otherwise the ceiling holds for the call and not for
    # the run, which is the same as not holding.
    ctx = RunContext(P, RunLimits(max_wall_clock_seconds=60, default_tool_timeout_seconds=30))
    _spend(ctx, 58)
    assert ctx.timeout_for(30) <= 2.01


# ------------------------------------------------------------------- the loop
async def test_a_run_out_of_time_stops_before_it_plans():
    ctx = RunContext(P, RunLimits(max_wall_clock_seconds=10))
    _spend(ctx, 11)
    planner = ScriptedPlanner(Finish(output="should never be asked"))

    result = await run_agent("go", planner, ToolRegistry(), ctx)

    assert result.outcome is RunOutcome.TIME_LIMIT
    assert result.steps == ()


async def test_a_hanging_tool_is_an_observation_not_a_hang():
    # The whole point: the run survives a downstream that never answers, and
    # the planner gets to try something else.
    registry = _registry(ToolSpec(name="slow", description="slow", parameters=OBJ), _hang)
    planner = ScriptedPlanner(
        CallTool(tool="slow", arguments={"q": "x"}),
        Finish(output="recovered", rationale="took another route"),
    )
    ctx = RunContext(P, RunLimits(max_wall_clock_seconds=5, default_tool_timeout_seconds=0.05))

    result = await run_agent("go", planner, registry, ctx)

    assert result.outcome is RunOutcome.COMPLETED
    assert result.output == "recovered"
    (call,) = result.tool_calls
    assert call.failed
    assert "timed out" in call.error


async def test_a_tools_own_timeout_beats_the_run_default():
    registry = _registry(
        ToolSpec(name="slow", description="slow", parameters=OBJ, timeout_seconds=0.05),
        _hang,
    )
    planner = ScriptedPlanner(
        CallTool(tool="slow", arguments={"q": "x"}),
        Finish(output="done"),
    )
    # The run default is a hundred times the tool's own. The tool's wins, so
    # this returns promptly rather than in five seconds.
    ctx = RunContext(P, RunLimits(max_wall_clock_seconds=30, default_tool_timeout_seconds=5))

    result = await run_agent("go", planner, registry, ctx)

    assert result.outcome is RunOutcome.COMPLETED
    (call,) = result.tool_calls
    assert call.duration_ms < 2000


async def test_repeated_timeouts_reach_the_give_up_ceiling():
    # A timeout counts as a failed action. A downstream that is down rather
    # than slow must not consume the whole step budget one timeout at a time.
    registry = _registry(ToolSpec(name="slow", description="slow", parameters=OBJ), _hang)
    planner = ScriptedPlanner(*[CallTool(tool="slow", arguments={"q": "x"}) for _ in range(5)])
    ctx = RunContext(
        P,
        RunLimits(
            max_consecutive_failures=2,
            max_wall_clock_seconds=5,
            default_tool_timeout_seconds=0.02,
        ),
    )

    result = await run_agent("go", planner, registry, ctx)

    assert result.outcome is RunOutcome.GAVE_UP
    assert len(result.tool_calls) == 2


class _HangingPlanner:
    """A planner that never answers -- an ordinary model call, on a bad day."""

    name = "hanging"

    async def decide(self, state):
        await asyncio.sleep(60)
        return Finish(output="never")  # pragma: no cover


async def test_a_hanging_planner_ends_the_run_on_time():
    ctx = RunContext(P, RunLimits(max_wall_clock_seconds=0.05, default_tool_timeout_seconds=0.05))

    result = await run_agent("go", _HangingPlanner(), ToolRegistry(), ctx)

    assert result.outcome is RunOutcome.TIME_LIMIT
    # Recorded, not merely returned. A run that stopped for a reason nobody
    # wrote down is indistinguishable from a run that crashed.
    (step,) = result.steps
    assert step.kind is StepKind.PLAN
    assert "timed out" in step.summary


# ------------------------------------------------------------------ the trace
async def test_the_trace_carries_the_time_bounds_that_were_in_force():
    registry = _registry(ToolSpec(name="t", description="t", parameters=OBJ), _ok)
    planner = ScriptedPlanner(CallTool(tool="t", arguments={"q": "x"}), Finish(output="ok"))
    ctx = RunContext(P, RunLimits(max_wall_clock_seconds=42, default_tool_timeout_seconds=7))

    result = await run_agent("go", planner, registry, ctx)
    trace = record_trace("go", ctx, result, registry)

    restored = RunTrace.from_dict(trace.to_dict())
    assert restored.limits.max_wall_clock_seconds == 42
    assert restored.limits.default_tool_timeout_seconds == 7


async def test_a_trace_recorded_before_time_bounds_existed_still_loads():
    # An audit record that cannot read its own history is not an audit record.
    registry = _registry(ToolSpec(name="t", description="t", parameters=OBJ), _ok)
    planner = ScriptedPlanner(Finish(output="ok"))
    ctx = RunContext(P)
    result = await run_agent("go", planner, registry, ctx)
    data = record_trace("go", ctx, result, registry).to_dict()

    del data["limits"]["max_wall_clock_seconds"]
    del data["limits"]["default_tool_timeout_seconds"]

    restored = RunTrace.from_dict(data)
    assert restored.limits.max_wall_clock_seconds == 300.0
    assert restored.limits.default_tool_timeout_seconds == 30.0
