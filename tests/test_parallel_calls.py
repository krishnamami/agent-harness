"""Parallel tool calls.

Every current model tool-use API returns more than one call per turn. A loop
that can only carry one forces three independent lookups into three planning
turns, and pays three times the latency for work that has no ordering between
it at all.

The interesting part is not the concurrency. It is what stays true once calls
stop happening one at a time: the batch is still afforded as a whole, still
recorded in the order it was planned, still redacted by the audit policy, and
still replays to the same thing.
"""

from __future__ import annotations

import asyncio

import pytest

from harness import (
    CallTool,
    CallTools,
    Finish,
    Principal,
    RefuseAll,
    RiskTier,
    RoleBasedAuthorization,
    RunContext,
    RunLimits,
    RunOutcome,
    RunTrace,
    ScriptedPlanner,
    StepKind,
    ToolRegistry,
    ToolSpec,
    record_trace,
    replay,
    run_agent,
)
from harness.trace import TraceError

OBJ = {"type": "object", "properties": {"n": {"type": "number"}}}
P = Principal(id="u1", roles=frozenset({"analyst"}))


def _batch(*names: str, **kw) -> CallTools:
    return CallTools(calls=tuple(CallTool(tool=n, arguments={}) for n in names), **kw)


class _Concurrency:
    """A tool that reports the high-water mark of simultaneous callers."""

    def __init__(self, hold: float = 0.05) -> None:
        self.hold = hold
        self.now = 0
        self.peak = 0

    async def __call__(self, arguments):
        self.now += 1
        self.peak = max(self.peak, self.now)
        try:
            await asyncio.sleep(self.hold)
            return {"ok": True}
        finally:
            self.now -= 1


async def _ok(arguments):
    return {"ok": True}


async def _boom(arguments):
    raise RuntimeError("downstream is down")


async def _hang(arguments):
    await asyncio.sleep(60)  # pragma: no cover
    return {}  # pragma: no cover


def _registry(**tools) -> ToolRegistry:
    r = ToolRegistry()
    for name, fn in tools.items():
        r.register(ToolSpec(name=name, description=name, parameters=OBJ), fn)
    return r


# ------------------------------------------------------------------- the type
def test_an_empty_batch_is_not_a_batch():
    with pytest.raises(ValueError, match="at least one call"):
        CallTools(calls=())


def test_a_batch_costs_the_sum_of_its_calls():
    batch = CallTools(
        calls=(
            CallTool(tool="a", arguments={}, estimated_cost_usd=0.10),
            CallTool(tool="b", arguments={}, estimated_cost_usd=0.25),
        )
    )
    assert batch.estimated_cost_usd == pytest.approx(0.35)


def test_limits_reject_a_fan_out_below_one():
    with pytest.raises(ValueError, match="max_parallel_calls"):
        RunLimits(max_parallel_calls=0)


# ------------------------------------------------------------- the concurrency
async def test_the_calls_actually_run_at_the_same_time():
    # Asserted on a high-water mark rather than on elapsed time: a wall-clock
    # assertion is a test that fails on a loaded CI runner for reasons that
    # have nothing to do with the code.
    tool = _Concurrency()
    planner = ScriptedPlanner(_batch("t", "t", "t"), Finish(output="done"))

    result = await run_agent("go", planner, _registry(t=tool), RunContext(P))

    assert result.outcome is RunOutcome.COMPLETED
    assert tool.peak == 3


async def test_results_are_recorded_in_the_order_planned_not_the_order_finished():
    # The whole reason `gather` is used rather than `as_completed`. Completion
    # order is a property of the network on the day; a trace that reflected it
    # would not replay to the same thing twice.
    async def _slow(arguments):
        await asyncio.sleep(0.05)
        return "slow"

    async def _fast(arguments):
        return "fast"

    registry = _registry(slow=_slow, fast=_fast)
    planner = ScriptedPlanner(_batch("slow", "fast"), Finish(output="done"))

    result = await run_agent("go", planner, registry, RunContext(P))

    assert [s.tool_name for s in result.tool_calls] == ["slow", "fast"]
    assert [s.result for s in result.tool_calls] == ["slow", "fast"]


async def test_a_batch_is_one_plan_step_and_one_tool_call_step_per_call():
    # A PLAN step per call would claim the planner took three turns, and a
    # replay built from that record would take three turns too.
    planner = ScriptedPlanner(_batch("t", "t", "t"), Finish(output="done"))
    result = await run_agent("go", planner, _registry(t=_ok), RunContext(P))

    plans = [s for s in result.steps if s.kind is StepKind.PLAN]
    assert len(plans) == 1
    assert plans[0].metadata["parallel"] is True
    assert len(plans[0].metadata["calls"]) == 3
    assert len(result.tool_calls) == 3


# -------------------------------------------------------------------- ceilings
async def test_a_batch_wider_than_the_ceiling_is_refused_and_the_run_continues():
    planner = ScriptedPlanner(
        _batch("t", "t", "t", "t"),
        Finish(output="split it up"),
    )
    ctx = RunContext(P, RunLimits(max_parallel_calls=2))

    result = await run_agent("go", planner, _registry(t=_ok), ctx)

    assert result.outcome is RunOutcome.COMPLETED
    (correction,) = [s for s in result.steps if s.kind is StepKind.CORRECTION]
    assert "exceeds" in correction.error
    assert result.tool_calls == ()


async def test_a_planner_that_keeps_over_asking_runs_out_of_patience():
    planner = ScriptedPlanner(*[_batch("t", "t", "t") for _ in range(6)])
    ctx = RunContext(P, RunLimits(max_parallel_calls=2, max_consecutive_failures=2))

    result = await run_agent("go", planner, _registry(t=_ok), ctx)

    assert result.outcome is RunOutcome.GAVE_UP


async def test_a_batch_that_does_not_fit_in_the_remaining_steps_is_not_half_run():
    # Running part of something planned as a unit leaves the planner reasoning
    # about a state that never existed.
    planner = ScriptedPlanner(_batch("t", "t", "t", "t"))
    ctx = RunContext(P, RunLimits(max_steps=3))

    result = await run_agent("go", planner, _registry(t=_ok), ctx)

    assert result.outcome is RunOutcome.STEP_LIMIT
    assert result.tool_calls == ()


async def test_cost_is_projected_across_the_whole_batch():
    # Each call is affordable on its own; together they are not. Checking them
    # one at a time would run two and then discover the problem.
    batch = CallTools(
        calls=(
            CallTool(tool="t", arguments={}, estimated_cost_usd=0.40),
            CallTool(tool="t", arguments={}, estimated_cost_usd=0.40),
            CallTool(tool="t", arguments={}, estimated_cost_usd=0.40),
        )
    )
    ctx = RunContext(P, RunLimits(max_cost_usd=1.00))

    result = await run_agent("go", ScriptedPlanner(batch), _registry(t=_ok), ctx)

    assert result.outcome is RunOutcome.COST_LIMIT
    assert result.tool_calls == ()


# --------------------------------------------------------------- authorisation
async def test_a_denied_call_does_not_stop_the_permitted_ones():
    # Unlike cost, a denial spends nothing. The planner learns both facts in
    # one turn instead of burning another on the half that was always going to
    # work.
    registry = ToolRegistry(
        authorization=RoleBasedAuthorization(
            {"open": frozenset({"analyst"}), "restricted": frozenset({"admin"})}
        )
    )
    registry.register(ToolSpec(name="open", description="o", parameters=OBJ), _ok)
    registry.register(ToolSpec(name="restricted", description="r", parameters=OBJ), _ok)

    planner = ScriptedPlanner(_batch("open", "restricted"), Finish(output="done"))
    result = await run_agent("go", planner, registry, RunContext(P))

    assert result.outcome is RunOutcome.COMPLETED
    by_tool = {s.tool_name: s for s in result.tool_calls}
    assert by_tool["restricted"].failed
    assert not by_tool["open"].failed


async def test_a_refusal_anywhere_in_a_batch_stops_the_whole_batch():
    # ADR-0008 makes a refusal terminal for the run, and "terminal except for
    # the other three calls asked for in the same breath" is not terminal.
    tool = _Concurrency()
    planner = ScriptedPlanner(_batch("t", "t", "t"))

    result = await run_agent("go", planner, _registry(t=tool), RunContext(P), gate=RefuseAll())

    assert result.outcome is RunOutcome.NOT_APPROVED
    assert tool.peak == 0


# -------------------------------------------------------------------- failures
async def test_one_failing_call_does_not_take_its_siblings_with_it():
    registry = _registry(good=_ok, bad=_boom)
    planner = ScriptedPlanner(_batch("good", "bad"), Finish(output="partial"))

    result = await run_agent("go", planner, registry, RunContext(P))

    assert result.outcome is RunOutcome.COMPLETED
    by_tool = {s.tool_name: s for s in result.tool_calls}
    assert by_tool["bad"].failed
    assert by_tool["good"].result == {"ok": True}


async def test_one_hanging_call_does_not_hold_up_its_siblings():
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="slow", description="s", parameters=OBJ, timeout_seconds=0.05), _hang
    )
    registry.register(ToolSpec(name="quick", description="q", parameters=OBJ), _ok)

    planner = ScriptedPlanner(_batch("slow", "quick"), Finish(output="done"))
    result = await run_agent("go", planner, registry, RunContext(P))

    assert result.outcome is RunOutcome.COMPLETED
    by_tool = {s.tool_name: s for s in result.tool_calls}
    assert "timed out" in by_tool["slow"].error
    assert not by_tool["quick"].failed
    # The quick call finished on its own schedule rather than waiting out the
    # slow one's timeout.
    assert by_tool["quick"].duration_ms < 40


# ---------------------------------------------------------------------- traces
async def test_a_batch_survives_a_trace_round_trip():
    planner = ScriptedPlanner(_batch("t", "t"), Finish(output="done"))
    ctx = RunContext(P)
    registry = _registry(t=_ok)
    result = await run_agent("go", planner, registry, ctx)

    restored = RunTrace.from_dict(record_trace("go", ctx, result, registry).to_dict())

    (plan,) = [s for s in restored.steps if s.kind is StepKind.PLAN]
    assert plan.metadata["parallel"] is True
    assert len(plan.metadata["calls"]) == 2


async def test_a_batched_run_replays():
    planner = ScriptedPlanner(_batch("a", "b"), Finish(output="done"))
    ctx = RunContext(P)
    registry = _registry(a=_ok, b=_ok)
    result = await run_agent("go", planner, registry, ctx)
    trace = record_trace("go", ctx, result, registry)

    report = await replay(trace)

    assert report.faithful


class _NoArgsFor:
    """An audit policy that withholds one tool's arguments."""

    name = "no-args-for"

    def __init__(self, tool: str) -> None:
        self.tool = tool

    def retention_days(self, tier: RiskTier) -> int:
        return 30

    def must_record_arguments(self, tool_name: str) -> bool:
        return tool_name != self.tool


async def test_the_audit_policy_reaches_inside_a_batch():
    # Left unhandled, batching would have been a way around the audit policy:
    # the same tool, the same arguments, recorded in full because they happened
    # to arrive in a list.
    calls = (
        CallTool(tool="open", arguments={"n": 1}),
        CallTool(tool="secret", arguments={"n": 2}),
    )
    ctx = RunContext(P)
    registry = _registry(open=_ok, secret=_ok)
    result = await run_agent(
        "go", ScriptedPlanner(CallTools(calls=calls), Finish(output="x")), registry, ctx
    )

    trace = record_trace("go", ctx, result, registry, audit=_NoArgsFor("secret"))

    (plan,) = [s for s in trace.steps if s.kind is StepKind.PLAN]
    recorded = {c["tool"]: c["arguments"] for c in plan.metadata["calls"]}
    assert recorded["open"] == {"n": 1}
    assert recorded["secret"] is None
    assert plan.metadata["arguments_withheld"] is True


async def test_a_batch_with_withheld_arguments_refuses_to_replay():
    calls = (CallTool(tool="secret", arguments={"n": 2}),)
    ctx = RunContext(P)
    registry = _registry(secret=_ok)
    result = await run_agent(
        "go", ScriptedPlanner(CallTools(calls=calls), Finish(output="x")), registry, ctx
    )
    trace = record_trace("go", ctx, result, registry, audit=_NoArgsFor("secret"))

    with pytest.raises(TraceError, match="withheld"):
        await replay(trace)


async def test_withholding_survives_a_round_trip():
    # It did not, before this change: metadata was dropped on serialisation, so
    # a reloaded trace replayed withheld arguments as an empty dict instead of
    # refusing. A guard that disappears when the trace is written to disk is
    # not a guard.
    ctx = RunContext(P)
    registry = _registry(secret=_ok)
    result = await run_agent(
        "go",
        ScriptedPlanner(CallTool(tool="secret", arguments={"n": 2}), Finish(output="x")),
        registry,
        ctx,
    )
    trace = record_trace("go", ctx, result, registry, audit=_NoArgsFor("secret"))

    restored = RunTrace.from_dict(trace.to_dict())

    with pytest.raises(TraceError, match="withheld"):
        await replay(restored)
