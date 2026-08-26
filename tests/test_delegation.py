"""Delegation.

A tree of agents is where an agent platform stops being a loop and starts being
something an enterprise has to reason about. Almost all of these tests are
about what a child is *not* allowed to have.
"""

from __future__ import annotations

import asyncio

import pytest

from harness import (
    CallTool,
    Finish,
    Principal,
    PrivilegeEscalationError,
    RefuseAll,
    RunContext,
    RunLimits,
    RunOutcome,
    RunResult,
    RunTrace,
    ScriptedPlanner,
    StepKind,
    StepRecord,
    SubRun,
    ToolRegistry,
    ToolSpec,
    delegate,
    new_child_context,
    record_trace,
)

OBJ = {"type": "object", "properties": {}}
BOSS = Principal(id="coordinator", roles=frozenset({"read", "write", "admin"}))


async def _ok(arguments):
    return {"ok": True}


async def _hang(arguments):
    await asyncio.sleep(60)  # pragma: no cover
    return {}  # pragma: no cover


def _registry(fn=_ok) -> ToolRegistry:
    r = ToolRegistry()
    r.register(ToolSpec(name="t", description="t", parameters=OBJ), fn)
    return r


def _parent(**kw) -> RunContext:
    return RunContext(BOSS, RunLimits(**kw))


def _age(ctx: RunContext, seconds: float) -> None:
    ctx._started_monotonic -= seconds


def _charge(ctx: RunContext, usd: float) -> None:
    ctx.record(
        StepRecord(index=ctx.step_count, kind=StepKind.OBSERVATION, summary="setup", cost_usd=usd)
    )


def _as_result(ctx: RunContext, _unused=None) -> RunResult:
    """The parent's own RunResult, as the executor would have built it.

    These tests drive `delegate` directly rather than through a planner, so
    there is no `run_agent` to produce one.
    """
    return RunResult(
        run_id=ctx.run_id,
        outcome=RunOutcome.COMPLETED,
        steps=tuple(ctx.steps),
        cost_usd=ctx.spent_usd,
    )


# ------------------------------------------------------------------ narrowing
async def test_a_child_inherits_the_parents_principal_by_default():
    parent = _parent()
    child = new_child_context(parent)
    assert child.principal == BOSS


async def test_a_child_may_drop_roles():
    parent = _parent()
    child = new_child_context(parent, principal=Principal(id="worker", roles=frozenset({"read"})))
    assert child.principal.roles == frozenset({"read"})


async def test_a_child_may_not_hold_a_role_the_parent_lacks():
    # The obvious attack on any agent tree: ask for more on the way down.
    parent = _parent()
    with pytest.raises(PrivilegeEscalationError, match="superuser"):
        new_child_context(
            parent, principal=Principal(id="worker", roles=frozenset({"read", "superuser"}))
        )


async def test_a_child_may_declare_a_purpose_where_the_parent_declared_none():
    # Under permissible-purpose authorisation a principal with no declared
    # purpose is denied outright, so declaring one narrows rather than widens.
    parent = _parent()
    child = new_child_context(
        parent, principal=Principal(id="w", roles=frozenset({"read"}), purpose="account_review")
    )
    assert child.principal.purpose == "account_review"


async def test_a_child_may_not_change_a_purpose_the_parent_declared():
    scoped = Principal(id="c", roles=frozenset({"read"}), purpose="account_review")
    parent = RunContext(scoped, RunLimits())
    with pytest.raises(PrivilegeEscalationError, match="purpose changed"):
        new_child_context(
            parent, principal=Principal(id="w", roles=frozenset({"read"}), purpose="collections")
        )


async def test_a_child_may_not_invent_an_attribute():
    parent = _parent()
    with pytest.raises(PrivilegeEscalationError, match="region"):
        new_child_context(
            parent, principal=Principal(id="w", roles=frozenset(), attributes={"region": "eu"})
        )


async def test_a_child_may_not_contradict_a_parent_attribute():
    scoped = Principal(id="c", roles=frozenset({"read"}), attributes={"region": "us"})
    parent = RunContext(scoped, RunLimits())
    with pytest.raises(PrivilegeEscalationError, match="differs"):
        new_child_context(
            parent,
            principal=Principal(id="w", roles=frozenset({"read"}), attributes={"region": "eu"}),
        )


# --------------------------------------------------------------------- budget
async def test_cost_is_drawn_from_what_the_parent_has_left():
    parent = _parent(max_cost_usd=1.00)
    _charge(parent, 0.90)

    child = new_child_context(parent, limits=RunLimits(max_cost_usd=5.00))

    assert child.limits.max_cost_usd == pytest.approx(0.10)


async def test_a_child_cannot_spend_past_the_parents_ceiling():
    # The same invariant, observed from the outside: the child is stopped by a
    # ceiling it never asked for.
    parent = _parent(max_cost_usd=1.00)
    _charge(parent, 0.90)

    result = await delegate(
        parent,
        "spend freely",
        ScriptedPlanner(CallTool(tool="t", arguments={}, estimated_cost_usd=0.50)),
        _registry(),
        limits=RunLimits(max_cost_usd=5.00),
    )

    assert result.outcome is RunOutcome.COST_LIMIT


async def test_time_is_drawn_from_what_the_parent_has_left():
    parent = _parent(max_wall_clock_seconds=60)
    _age(parent, 58)

    child = new_child_context(parent, limits=RunLimits(max_wall_clock_seconds=300))

    assert child.limits.max_wall_clock_seconds <= 2.01


async def test_steps_are_not_drawn_because_they_are_not_fungible():
    # A child's turns are not the parent's turns. The parent has one step left
    # and the child still gets its own twenty.
    parent = _parent(max_steps=3)
    _charge(parent, 0.0)
    _charge(parent, 0.0)

    child = new_child_context(parent, limits=RunLimits(max_steps=20))

    assert child.limits.max_steps == 20


async def test_a_child_runs_more_steps_than_the_parent_had_remaining():
    parent = _parent(max_steps=3)
    _charge(parent, 0.0)
    _charge(parent, 0.0)

    result = await delegate(
        parent,
        "do a lot",
        ScriptedPlanner(
            *[CallTool(tool="t", arguments={}) for _ in range(5)],
            Finish(output="done"),
        ),
        _registry(),
        limits=RunLimits(max_steps=20),
    )

    assert result.outcome is RunOutcome.COMPLETED
    assert len(result.tool_calls) == 5


# ---------------------------------------------------------------------- depth
async def test_a_child_is_one_deeper_than_its_parent():
    parent = _parent()
    assert new_child_context(parent).depth == 1


async def test_delegation_stops_at_the_depth_ceiling():
    parent = RunContext(BOSS, RunLimits(max_delegation_depth=2), depth=2)
    assert new_child_context(parent) is RunOutcome.DEPTH_LIMIT


async def test_a_branch_cannot_buy_itself_more_depth_on_the_way_down():
    # The ceiling belongs to the tree. A child asking for a deeper limit gets
    # the parent's, or a long enough chain could raise its own roof.
    parent = _parent(max_delegation_depth=2)
    child = new_child_context(parent, limits=RunLimits(max_delegation_depth=99))
    assert child.limits.max_delegation_depth == 2


async def test_the_ceiling_holds_across_a_real_chain():
    parent = _parent(max_delegation_depth=2)
    a = new_child_context(parent)
    b = new_child_context(a)
    assert b.depth == 2
    assert new_child_context(b) is RunOutcome.DEPTH_LIMIT


async def test_a_refused_delegation_is_recorded_not_silent():
    parent = RunContext(BOSS, RunLimits(max_delegation_depth=0))

    result = await delegate(parent, "go deeper", ScriptedPlanner(Finish(output="x")), _registry())

    assert result.outcome is RunOutcome.DEPTH_LIMIT
    (step,) = parent.steps
    assert step.kind is StepKind.DELEGATION
    assert step.failed


# -------------------------------------------------------------- the parent's record
async def test_a_delegation_is_one_step_in_the_parent_not_the_childs_many():
    parent = _parent()

    await delegate(
        parent,
        "sub",
        ScriptedPlanner(
            CallTool(tool="t", arguments={}),
            CallTool(tool="t", arguments={}),
            CallTool(tool="t", arguments={}),
            Finish(output="done"),
        ),
        _registry(),
    )

    assert len(parent.steps) == 1
    (step,) = parent.steps
    assert step.kind is StepKind.DELEGATION
    assert step.metadata["child_steps"] > 3


async def test_the_childs_cost_is_charged_to_the_parent():
    # Otherwise a tree spends its way around the root's ceiling by delegating.
    parent = _parent(max_cost_usd=1.00)

    await delegate(parent, "sub", ScriptedPlanner(Finish(output="x", cost_usd=0.25)), _registry())

    assert parent.spent_usd == pytest.approx(0.25)
    assert parent.remaining_usd == pytest.approx(0.75)


async def test_the_parent_keeps_the_childs_result():
    parent = _parent()
    result = await delegate(parent, "sub", ScriptedPlanner(Finish(output="x")), _registry())
    (sub,) = parent.sub_runs
    assert sub.result == result
    assert sub.context.depth == 1


async def test_the_delegation_step_links_to_the_child_run():
    parent = _parent()
    result = await delegate(parent, "sub", ScriptedPlanner(Finish(output="x")), _registry())
    (step,) = parent.steps
    assert step.metadata["child_run_id"] == result.run_id


async def test_the_child_context_links_back_to_the_parent_step():
    parent = _parent()
    _charge(parent, 0.0)
    child = new_child_context(parent)
    assert child.metadata["parent_run_id"] == parent.run_id
    assert child.metadata["parent_step_index"] == 1


# ------------------------------------------------------------------- failures
async def test_a_failed_sub_run_is_one_failure_not_one_per_child_step():
    # The reason a delegation is a single step. Counting the child's failures
    # individually would trip the parent's give-up ceiling on one bad sub-run.
    parent = _parent(max_consecutive_failures=3)

    async def _boom(arguments):
        raise RuntimeError("downstream is down")

    await delegate(
        parent,
        "sub",
        ScriptedPlanner(*[CallTool(tool="t", arguments={}) for _ in range(5)]),
        _registry(_boom),
        limits=RunLimits(max_consecutive_failures=2),
    )

    assert parent.consecutive_failures == 1
    assert not parent.should_give_up()


async def test_a_sub_run_that_hit_a_ceiling_marks_the_parents_step_failed():
    parent = _parent()

    result = await delegate(
        parent,
        "sub",
        ScriptedPlanner(*[CallTool(tool="t", arguments={}) for _ in range(9)]),
        _registry(_hang),
        limits=RunLimits(max_wall_clock_seconds=0.05, default_tool_timeout_seconds=0.02),
    )

    assert not result.succeeded
    (step,) = parent.steps
    assert step.failed
    assert "sub-run" in step.error


async def test_a_refusal_is_terminal_for_the_sub_run_and_an_observation_to_the_parent():
    # ADR-0012. A human said no to *that* delegation, not to the goal: the
    # sub-run ends there, and the parent is free to try another route. The
    # opposing design -- a refusal anywhere kills the tree -- makes any single
    # cautious reviewer a denial of service on the whole run.
    parent = _parent()

    first = await delegate(
        parent,
        "the route that gets refused",
        ScriptedPlanner(CallTool(tool="t", arguments={})),
        _registry(),
        gate=RefuseAll(),
    )
    second = await delegate(
        parent, "another route", ScriptedPlanner(Finish(output="worked")), _registry()
    )

    assert first.outcome is RunOutcome.NOT_APPROVED
    assert second.succeeded
    assert second.output == "worked"
    assert len(parent.steps) == 2
    assert parent.steps[0].failed
    assert not parent.steps[1].failed
    # One refusal is one failure, and the parent is nowhere near giving up.
    assert parent.consecutive_failures == 0


async def test_a_parent_with_nothing_left_starts_no_child():
    parent = _parent(max_cost_usd=1.00)
    _charge(parent, 1.00)

    result = await delegate(parent, "sub", ScriptedPlanner(Finish(output="x")), _registry())

    assert result.outcome is RunOutcome.COST_LIMIT
    assert parent.sub_runs == []


async def test_a_parent_out_of_steps_starts_no_child():
    parent = _parent(max_steps=1)
    _charge(parent, 0.0)

    result = await delegate(parent, "sub", ScriptedPlanner(Finish(output="x")), _registry())

    assert result.outcome is RunOutcome.STEP_LIMIT
    assert parent.sub_runs == []


# -------------------------------------------------------------- the tree's record
async def test_a_delegated_run_can_be_traced():
    # The defect this closes. Keeping only the child's `RunResult` left its
    # limits, principal and tier on a context nobody held any more -- so a
    # delegated run could not be turned into a trace at all, and ADR-0011
    # quietly contradicted ADR-0007 from the inside.
    registry = _registry()
    parent = _parent()
    result = await delegate(
        parent,
        "check the income documents",
        ScriptedPlanner(CallTool(tool="t", arguments={}), Finish(output="cleared")),
        registry,
    )

    trace = record_trace("resolve the file", parent, _as_result(parent, result), registry)

    (child,) = trace.sub_traces
    assert child.goal == "check the income documents"
    assert child.run_id == result.run_id
    assert child.outcome is RunOutcome.COMPLETED
    assert len(child.steps) == len(result.steps)


async def test_the_whole_tree_survives_serialisation():
    registry = _registry()
    parent = _parent()
    result = await delegate(parent, "sub", ScriptedPlanner(Finish(output="x")), registry)
    trace = record_trace("root", parent, _as_result(parent, result), registry)

    restored = RunTrace.from_dict(trace.to_dict())

    assert len(restored.sub_traces) == 1
    assert restored.sub_traces[0].run_id == result.run_id


async def test_walk_reaches_every_run_in_the_tree():
    # Replay works on one run at a time -- the harness never chose to delegate,
    # the service did -- so what it guarantees is that every run in the tree is
    # individually reachable and individually replayable.
    registry = _registry()
    parent = _parent()
    a = new_child_context(parent)
    await delegate(a, "grandchild", ScriptedPlanner(Finish(output="deep")), registry)
    await delegate(parent, "child", ScriptedPlanner(Finish(output="x")), registry)
    parent.sub_runs.append(SubRun(context=a, result=_as_result(a, None)))

    trace = record_trace("root", parent, _as_result(parent, None), registry)

    assert len(list(trace.walk())) == 4


async def test_a_trace_recorded_before_delegation_existed_still_loads():
    registry = _registry()
    parent = _parent()
    result = await delegate(parent, "sub", ScriptedPlanner(Finish(output="x")), registry)
    data = record_trace("root", parent, _as_result(parent, result), registry).to_dict()

    del data["sub_traces"]

    assert RunTrace.from_dict(data).sub_traces == ()
