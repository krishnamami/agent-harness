"""The loop.

Two things are being tested: that a run does what the planner asked, and that
it stops when it must. The second matters more.
"""

from __future__ import annotations

import pytest

from harness import (
    CallTool,
    Finish,
    Principal,
    RiskTier,
    RoleBasedAuthorization,
    RunContext,
    RunLimits,
    RunOutcome,
    ScriptedPlanner,
    StepKind,
    ToolRegistry,
    ToolSpec,
    run_agent,
)

OBJ = {"type": "object", "properties": {"q": {"type": "string"}}}
P = Principal(id="u1", roles=frozenset({"analyst"}))


def _registry(**tools):
    r = ToolRegistry()
    for name, fn in tools.items():
        r.register(ToolSpec(name=name, description=name, parameters=OBJ), fn)
    return r


async def _ok(arguments):
    return {"ok": True, **arguments}


async def _boom(arguments):
    raise RuntimeError("downstream is down")


# ------------------------------------------------------------------ happy path
async def test_a_run_completes_and_returns_the_output():
    planner = ScriptedPlanner(
        CallTool(tool="search", arguments={"q": "x"}, rationale="look it up"),
        Finish(output="the answer", rationale="done"),
    )
    result = await run_agent("find x", planner, _registry(search=_ok), RunContext(P))

    assert result.outcome is RunOutcome.COMPLETED
    assert result.output == "the answer"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].result == {"ok": True, "q": "x"}


async def test_every_step_is_recorded_in_order():
    planner = ScriptedPlanner(CallTool(tool="search", arguments={}), Finish(output=None))
    result = await run_agent("g", planner, _registry(search=_ok), RunContext(P))
    kinds = [s.kind for s in result.steps]
    assert kinds == [StepKind.PLAN, StepKind.TOOL_CALL, StepKind.FINISH]
    assert [s.index for s in result.steps] == [0, 1, 2]


# ------------------------------------------------------------------- bounds
async def test_a_looping_agent_hits_the_step_ceiling():
    """The answer to 'what stops it running away'."""
    planner = ScriptedPlanner(*[CallTool(tool="search", arguments={}) for _ in range(50)])
    result = await run_agent(
        "loop", planner, _registry(search=_ok), RunContext(P, RunLimits(max_steps=6))
    )
    assert result.outcome is RunOutcome.STEP_LIMIT
    assert len(result.steps) <= 6


async def test_a_call_that_would_breach_the_budget_is_refused_before_it_runs():
    """Not detected afterwards — refused."""
    called: list[str] = []

    async def expensive(arguments):
        called.append("ran")
        return {}

    planner = ScriptedPlanner(CallTool(tool="expensive", arguments={}, estimated_cost_usd=5.00))
    result = await run_agent(
        "g", planner, _registry(expensive=expensive), RunContext(P, RunLimits(max_cost_usd=0.50))
    )
    assert result.outcome is RunOutcome.COST_LIMIT
    assert called == [], "the tool ran despite breaching the ceiling"


async def test_cost_accumulates_across_steps():
    planner = ScriptedPlanner(
        CallTool(tool="search", arguments={}, estimated_cost_usd=0.20),
        CallTool(tool="search", arguments={}, estimated_cost_usd=0.20),
        CallTool(tool="search", arguments={}, estimated_cost_usd=0.20),
    )
    result = await run_agent(
        "g", planner, _registry(search=_ok), RunContext(P, RunLimits(max_cost_usd=0.50))
    )
    assert result.outcome is RunOutcome.COST_LIMIT
    assert result.cost_usd == pytest.approx(0.40)


# -------------------------------------------------------------- self-correction
async def test_a_tool_failure_is_fed_back_rather_than_ending_the_run():
    """A flaky downstream is what self-correction is for."""
    planner = ScriptedPlanner(
        CallTool(tool="flaky", arguments={}),
        CallTool(tool="search", arguments={}),
        Finish(output="recovered"),
    )
    r = ToolRegistry()
    r.register(ToolSpec(name="flaky", description="f", parameters=OBJ), _boom)
    r.register(ToolSpec(name="search", description="s", parameters=OBJ), _ok)

    result = await run_agent("g", planner, r, RunContext(P))
    assert result.outcome is RunOutcome.COMPLETED
    assert result.output == "recovered"
    assert any(s.failed for s in result.steps)


async def test_the_planner_can_see_what_went_wrong():
    """PlannerState.last_error is the whole of self-correction from the planner's side."""
    seen: list[str | None] = []

    class Watching:
        name = "watching"

        def __init__(self):
            self._n = 0

        async def decide(self, state):
            seen.append(state.last_error)
            self._n += 1
            if self._n == 1:
                return CallTool(tool="flaky", arguments={})
            return Finish(output=None)

    r = ToolRegistry()
    r.register(ToolSpec(name="flaky", description="f", parameters=OBJ), _boom)
    await run_agent("g", Watching(), r, RunContext(P))

    assert seen[0] is None
    assert seen[1] is not None
    assert "downstream is down" in seen[1]


async def test_repeated_failure_gives_up_rather_than_retrying_forever():
    """Self-correction without a give-up condition is failing slowly and expensively."""
    planner = ScriptedPlanner(*[CallTool(tool="flaky", arguments={}) for _ in range(20)])
    r = ToolRegistry()
    r.register(ToolSpec(name="flaky", description="f", parameters=OBJ), _boom)

    result = await run_agent("g", planner, r, RunContext(P, RunLimits(max_consecutive_failures=3)))
    assert result.outcome is RunOutcome.GAVE_UP
    assert len(result.tool_calls) == 3


# ------------------------------------------------------------------- policy
async def test_the_planner_is_only_offered_tools_it_may_call():
    """It has no reference to the registry, so it cannot reach past this."""
    offered: list[list[str]] = []

    class Nosy:
        name = "nosy"

        async def decide(self, state):
            offered.append([t["name"] for t in state.tools])
            return Finish(output=None)

    r = ToolRegistry(
        RoleBasedAuthorization(
            {
                "search": frozenset({"analyst"}),
                "payments": frozenset({"treasury"}),
            }
        )
    )
    r.register(ToolSpec(name="search", description="s", parameters=OBJ), _ok)
    r.register(ToolSpec(name="payments", description="p", parameters=OBJ), _ok)

    await run_agent("g", Nosy(), r, RunContext(P))
    assert offered[0] == ["search"]


async def test_a_denied_call_is_recorded_and_counts_toward_giving_up():
    """An agent that probes denials indefinitely is a security problem."""
    planner = ScriptedPlanner(*[CallTool(tool="payments", arguments={}) for _ in range(10)])
    r = ToolRegistry(RoleBasedAuthorization({"payments": frozenset({"treasury"})}))
    r.register(ToolSpec(name="payments", description="p", parameters=OBJ), _ok)

    result = await run_agent("g", planner, r, RunContext(P, RunLimits(max_consecutive_failures=2)))
    assert result.outcome is RunOutcome.GAVE_UP
    assert all("denied" in s.error for s in result.tool_calls)


async def test_a_planner_that_raises_ends_the_run():
    """Retrying a planner that just crashed spends a budget on stack traces."""

    class Broken:
        name = "broken"

        async def decide(self, state):
            raise ValueError("bad prompt template")

    result = await run_agent("g", Broken(), _registry(), RunContext(P))
    assert result.outcome is RunOutcome.FAILED
    assert "ValueError" in result.error


async def test_an_unknown_tool_is_an_observation_not_a_crash():
    planner = ScriptedPlanner(CallTool(tool="does_not_exist", arguments={}), Finish(output="ok"))
    result = await run_agent("g", planner, _registry(search=_ok), RunContext(P))
    assert result.outcome is RunOutcome.COMPLETED


# ------------------------------------------------------------------ context
async def test_the_planner_knows_what_budget_is_left():
    """A planner with one step left can finish rather than start something it cannot end."""
    seen: list[tuple[int, float]] = []

    class Budget:
        name = "budget"

        async def decide(self, state):
            seen.append((state.remaining_steps, state.remaining_usd))
            return Finish(output=None)

    await run_agent(
        "g", Budget(), _registry(), RunContext(P, RunLimits(max_steps=9, max_cost_usd=2.0))
    )
    assert seen[0] == (9, 2.0)


async def test_the_run_carries_its_tier():
    ctx = RunContext(P, tier=RiskTier.CONSEQUENTIAL)
    await run_agent("g", ScriptedPlanner(Finish(output=None)), _registry(), ctx)
    assert ctx.tier is RiskTier.CONSEQUENTIAL
