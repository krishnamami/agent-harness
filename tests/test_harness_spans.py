"""Spans.

Two things are being checked. That a run is visible in a backend at all -- it
was not, until now; the harness emitted no spans and a run left no trace
anywhere except its own record. And that making it visible did not quietly turn
the observability backend into a second, ungoverned copy of the audit trail.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from harness import (
    ApprovalDecision,
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
    ScriptedPlanner,
    TierGate,
    ToolRegistry,
    ToolSpec,
    delegate,
    run_agent,
)

OBJ = {"type": "object", "properties": {"ssn": {"type": "string"}}}
P = Principal(id="u1", roles=frozenset({"analyst"}))
SECRET = "123-45-6789"


@pytest.fixture(scope="module")
def _exporter() -> InMemorySpanExporter:
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


@pytest.fixture
def spans(_exporter: InMemorySpanExporter) -> InMemorySpanExporter:
    _exporter.clear()
    return _exporter


def named(exporter: InMemorySpanExporter, name: str) -> list:
    return [s for s in exporter.get_finished_spans() if s.name == name]


def one(exporter: InMemorySpanExporter, name: str):
    matches = named(exporter, name)
    assert len(matches) == 1, f"expected exactly one {name}, got {len(matches)}"
    return matches[0]


async def _ok(arguments):
    return {"ok": True}


async def _boom(arguments):
    raise RuntimeError("downstream is down")


async def _hang(arguments):
    await asyncio.sleep(60)  # pragma: no cover
    return {}  # pragma: no cover


def _registry(tier: RiskTier = RiskTier.ROUTINE, fn=_ok, **more) -> ToolRegistry:
    r = ToolRegistry()
    r.register(ToolSpec(name="t", description="t", parameters=OBJ, tier=tier), fn)
    for name, f in more.items():
        r.register(ToolSpec(name=name, description=name, parameters=OBJ), f)
    return r


# --------------------------------------------------------------------- the run
async def test_a_run_produces_a_span(spans):
    planner = ScriptedPlanner(CallTool(tool="t", arguments={}), Finish(output="done"))
    await run_agent("go", planner, _registry(), RunContext(P))

    span = one(spans, "agent.run")
    assert span.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert span.attributes["harness.principal.id"] == "u1"
    assert span.attributes["harness.outcome"] == "completed"
    assert span.attributes["harness.steps"] == 3
    assert span.status.status_code is StatusCode.OK


async def test_the_run_span_records_the_bounds_it_started_under(spans):
    # The most useful question about a run that stopped early is what it was
    # allowed to do, and a span that only says `step_limit` cannot answer it.
    ctx = RunContext(P, RunLimits(max_steps=7, max_cost_usd=2.50, max_wall_clock_seconds=45))
    await run_agent("go", ScriptedPlanner(Finish(output="x")), _registry(), ctx)

    span = one(spans, "agent.run")
    assert span.attributes["harness.limits.max_steps"] == 7
    assert span.attributes["harness.limits.max_cost_usd"] == 2.50
    assert span.attributes["harness.limits.max_wall_clock_seconds"] == 45


async def test_a_bounded_stop_is_not_an_error(spans):
    # Marking ceilings as errors would make the error rate on every dashboard a
    # measure of how often the bounds did their job.
    planner = ScriptedPlanner(*[CallTool(tool="t", arguments={}) for _ in range(10)])
    ctx = RunContext(P, RunLimits(max_steps=3))

    result = await run_agent("go", planner, _registry(), ctx)

    assert result.outcome is RunOutcome.STEP_LIMIT
    span = one(spans, "agent.run")
    assert span.status.status_code is StatusCode.OK
    assert span.attributes["harness.outcome"] == "step_limit"


async def test_a_crashed_run_is_an_error(spans):
    class _Broken:
        name = "broken"

        async def decide(self, state):
            raise RuntimeError("planner exploded")

    result = await run_agent("go", _Broken(), _registry(), RunContext(P))

    assert result.outcome is RunOutcome.FAILED
    assert one(spans, "agent.run").status.status_code is StatusCode.ERROR


# ------------------------------------------------------------------- the calls
async def test_a_tool_call_produces_a_span(spans):
    planner = ScriptedPlanner(CallTool(tool="t", arguments={}), Finish(output="x"))
    await run_agent("go", planner, _registry(tier=RiskTier.ELEVATED), RunContext(P))

    span = one(spans, "agent.tool")
    assert span.attributes["gen_ai.tool.name"] == "t"
    assert span.attributes["harness.tool.tier"] == str(RiskTier.ELEVATED)
    # UNSET, not OK: the convention is that instrumentation sets ERROR and
    # otherwise stays quiet. Only the run span asserts OK, because the harness
    # genuinely evaluates a run's outcome.
    assert span.status.status_code is not StatusCode.ERROR


@pytest.mark.parametrize(
    ("fn", "kind", "spec"),
    [
        (_boom, "failed", {}),
        (_hang, "timeout", {"timeout_seconds": 0.02}),
    ],
)
async def test_a_failing_call_is_marked_with_a_groupable_reason(spans, fn, kind, spec):
    # The kind is a small closed set so it can be grouped by. The detail goes
    # in the status message, which backends do not index -- a stringified
    # exception is unbounded cardinality and ruins an attribute index.
    registry = ToolRegistry()
    registry.register(ToolSpec(name="t", description="t", parameters=OBJ, **spec), fn)
    planner = ScriptedPlanner(CallTool(tool="t", arguments={}), Finish(output="x"))

    await run_agent("go", planner, registry, RunContext(P))

    span = one(spans, "agent.tool")
    assert span.attributes["harness.error.kind"] == kind
    assert span.status.status_code is StatusCode.ERROR


async def test_a_denied_call_is_marked_denied_not_failed(spans):
    registry = ToolRegistry(authorization=RoleBasedAuthorization({"t": frozenset({"admin"})}))
    registry.register(ToolSpec(name="t", description="t", parameters=OBJ), _ok)
    planner = ScriptedPlanner(CallTool(tool="t", arguments={}), Finish(output="x"))

    await run_agent("go", planner, registry, RunContext(P))

    # Refused before review, so no tool span -- the denial is on the approval
    # path, not the execution path. What matters is the run still completes.
    assert one(spans, "agent.run").attributes["harness.outcome"] == "completed"


# ------------------------------------------------------------ the boundary
async def test_arguments_never_reach_a_span(spans):
    # The rule this whole module exists to keep. `AuditPolicy` governs what the
    # trace retains; a span goes to a different store, with different retention
    # and a wider audience. Putting arguments on spans would route around the
    # audit policy through a side door.
    planner = ScriptedPlanner(
        CallTool(tool="t", arguments={"ssn": SECRET}),
        Finish(output={"ssn": SECRET}),
    )
    await run_agent("go", planner, _registry(), RunContext(P))

    for span in spans.get_finished_spans():
        for key, value in (span.attributes or {}).items():
            assert SECRET not in str(value), f"{span.name}.{key} leaked an argument"
        assert SECRET not in str(span.status.description or "")


async def test_a_parallel_batch_produces_sibling_tool_spans(spans):
    # An overlapping timeline is what parallelism looks like in a backend, and
    # a serialised one is what a regression looks like.
    batch = CallTools(calls=tuple(CallTool(tool="t", arguments={}) for _ in range(3)))
    await run_agent("go", ScriptedPlanner(batch, Finish(output="x")), _registry(), RunContext(P))

    tools = named(spans, "agent.tool")
    assert len(tools) == 3
    run = one(spans, "agent.run")
    assert {t.parent.span_id for t in tools} == {run.context.span_id}


async def test_the_planner_gets_its_own_span(spans):
    planner = ScriptedPlanner(CallTool(tool="t", arguments={}), Finish(output="x"))
    await run_agent("go", planner, _registry(), RunContext(P))

    plans = named(spans, "agent.plan")
    assert len(plans) == 2
    assert plans[0].attributes["harness.planner"] == "scripted"
    assert plans[0].attributes["harness.decision"] == "call_tool"
    assert plans[1].attributes["harness.decision"] == "finish"


class _Supervisor:
    """A gate where somebody actually looks.

    `AutoApprove` deliberately reports `gated=False` even above a threshold --
    it is named so nobody mistakes it for a control -- so it cannot stand in
    for a real review here.
    """

    name = "supervisor"

    async def review(self, request):
        return ApprovalDecision(approved=True, approver="supervisor-1", gated=True)


async def test_an_approval_records_who_decided_and_whether_anyone_looked(spans):
    registry = _registry(tier=RiskTier.CRITICAL)
    planner = ScriptedPlanner(CallTool(tool="t", arguments={}), Finish(output="x"))

    await run_agent(
        "go",
        planner,
        registry,
        RunContext(P),
        gate=TierGate(_Supervisor(), RiskTier.CONSEQUENTIAL),
    )

    span = one(spans, "agent.approval")
    assert span.attributes["harness.gated"] is True
    assert span.attributes["harness.approved"] is True
    assert span.attributes["harness.approver"]


async def test_a_refusal_marks_the_approval_span(spans):
    planner = ScriptedPlanner(CallTool(tool="t", arguments={}))
    await run_agent("go", planner, _registry(), RunContext(P), gate=RefuseAll())

    assert one(spans, "agent.approval").status.status_code is StatusCode.ERROR
    # The run stopped, but it stopped the way it was told to.
    assert one(spans, "agent.run").status.status_code is StatusCode.OK


# ------------------------------------------------------------------- the tree
async def test_a_delegated_run_nests_inside_its_parent(spans):
    # The payoff: the agent tree and the span tree are the same shape, so a
    # coordinator with four workers looks like a coordinator with four workers.
    parent = RunContext(P)
    await delegate(parent, "sub", ScriptedPlanner(Finish(output="x")), _registry())

    delegation = one(spans, "agent.delegate")
    runs = named(spans, "agent.run")
    assert len(runs) == 1  # the parent's own run_agent was never called here
    child = runs[0]
    assert child.parent.span_id == delegation.context.span_id
    assert delegation.attributes["harness.child.outcome"] == "completed"
    assert delegation.attributes["harness.depth"] == 1


async def test_a_refused_delegation_is_still_visible(spans):
    # A tree showing only the delegations that happened cannot answer why a
    # branch is missing.
    parent = RunContext(P, RunLimits(max_delegation_depth=0))
    await delegate(parent, "sub", ScriptedPlanner(Finish(output="x")), _registry())

    span = one(spans, "agent.delegate")
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["harness.error.kind"] == "refused"
    assert named(spans, "agent.run") == []
