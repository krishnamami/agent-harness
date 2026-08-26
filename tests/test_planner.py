from __future__ import annotations

import dataclasses

import pytest

from harness import CallTool, Finish, Planner, PlannerState, ScriptedPlanner
from harness.run import StepKind, StepRecord


def test_scripted_planner_needs_at_least_one_decision():
    with pytest.raises(ValueError, match="at least one"):
        ScriptedPlanner()


def test_scripted_planner_satisfies_the_protocol():
    assert isinstance(ScriptedPlanner(Finish(output=None)), Planner)


async def test_scripted_planner_returns_decisions_in_order():
    p = ScriptedPlanner(CallTool(tool="a", arguments={}), Finish(output="x"))
    state = PlannerState(goal="g", tools=[])
    assert isinstance(await p.decide(state), CallTool)
    assert isinstance(await p.decide(state), Finish)


async def test_running_off_the_end_of_a_script_finishes_visibly():
    """Finishing quietly would hide that the script did not anticipate the run."""
    p = ScriptedPlanner(Finish(output="x"))
    state = PlannerState(goal="g", tools=[])
    await p.decide(state)
    second = await p.decide(state)
    assert isinstance(second, Finish)
    assert "exhausted" in second.rationale


def test_last_error_is_none_when_the_previous_step_succeeded():
    ok = StepRecord(index=0, kind=StepKind.TOOL_CALL, summary="ok")
    assert PlannerState(goal="g", tools=[], history=(ok,)).last_error is None


def test_last_error_surfaces_the_most_recent_failure():
    bad = StepRecord(index=0, kind=StepKind.TOOL_CALL, summary="x", error="it broke")
    assert PlannerState(goal="g", tools=[], history=(bad,)).last_error == "it broke"


def test_planner_state_is_a_value_not_the_live_context():
    """A planner handed the context could mutate the budget constraining it."""
    state = PlannerState(goal="g", tools=[], remaining_usd=1.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.remaining_usd = 99.0  # type: ignore[misc]
