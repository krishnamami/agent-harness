"""The whole thesis, executed.

One agent. One set of tools. One executor. Two overlays.

If the harness is genuinely portable between regulatory contexts, then
swapping a module — not a fork, not a branch, not a config flag buried in the
core — changes what the same run is permitted to do. These tests are that
claim, run.
"""

from __future__ import annotations

from harness import (
    CallTool,
    Finish,
    Principal,
    RiskTier,
    RoleBasedAuthorization,
    RunContext,
    RunOutcome,
    ScriptedPlanner,
    StandardAudit,
    ToolRegistry,
    ToolSpec,
    record_trace,
    regulated_overlay,
    replay,
    run_agent,
)
from harness.gates import AutoApprove, TierGate
from harness.run import StepKind
from harness.trace import TraceError, decisions_from

OBJ = {"type": "object", "properties": {"consumer_id": {"type": "string"}}}

# The same two tools in both worlds. Reading a file is elevated; deciding
# something about a person is consequential.
TOOLS = {
    "read_file": RiskTier.ELEVATED,
    "score_applicant": RiskTier.CONSEQUENTIAL,
}


async def _tool(arguments):
    return {"consumer_id": arguments.get("consumer_id"), "value": 712}


def _registry(authorization=None):
    r = ToolRegistry(authorization)
    for name, tier in TOOLS.items():
        r.register(ToolSpec(name=name, description=name, parameters=OBJ, tier=tier), _tool)
    return r


def _planner():
    return ScriptedPlanner(
        CallTool(tool="read_file", arguments={"consumer_id": "c-42"}, rationale="pull the file"),
        CallTool(tool="score_applicant", arguments={"consumer_id": "c-42"}, rationale="score it"),
        Finish(output="approved", rationale="done"),
    )


# ============================================================ neutral world
async def test_the_neutral_overlay_lets_a_role_holder_through_unsupervised():
    analyst = Principal(id="analyst-7", roles=frozenset({"analyst"}))
    registry = _registry(RoleBasedAuthorization({name: frozenset({"analyst"}) for name in TOOLS}))

    result = await run_agent("assess", _planner(), registry, RunContext(analyst))

    assert result.outcome is RunOutcome.COMPLETED
    assert [s.tool_name for s in result.tool_calls] == ["read_file", "score_applicant"]
    assert [s for s in result.steps if s.kind is StepKind.APPROVAL] == []


# ========================================================== regulated world
def _regulated():
    return regulated_overlay(
        tool_purposes={
            "read_file": frozenset({"account-review", "credit-application"}),
            "score_applicant": frozenset({"credit-application"}),
        },
        reviewers=frozenset({"analyst-7", "supervisor-1"}),
        sensitive_tools=frozenset({"score_applicant"}),
        gate_from=RiskTier.CONSEQUENTIAL,
    )


async def test_the_same_run_is_refused_when_no_purpose_is_declared():
    """The identical principal, tools and plan — and nothing is permitted.

    Note what this does *not* assert. The run still reaches `COMPLETED`,
    because the planner was scripted to finish and two denials is under the
    give-up ceiling. A denial is an observation, not a run-ending event
    (ADR-0005) — so the meaningful assertion is that no tool actually ran,
    not that the run failed. An agent can complete having achieved nothing,
    and the trace is where you see that.
    """
    authorization, _, gate = _regulated()
    no_purpose = Principal(id="analyst-7", roles=frozenset({"analyst"}))

    result = await run_agent(
        "assess", _planner(), _registry(authorization), RunContext(no_purpose), gate=gate
    )

    assert len(result.tool_calls) == 2
    assert all(s.error is not None for s in result.tool_calls)
    assert all("no purpose declared" in s.error for s in result.tool_calls)
    assert all(s.result is None for s in result.tool_calls)


async def test_a_purpose_that_permits_one_tool_does_not_permit_the_other():
    """Authorisation per call, not per workflow."""
    authorization, _, gate = _regulated()
    reviewing = Principal(id="analyst-7", roles=frozenset({"analyst"}), purpose="account-review")

    result = await run_agent(
        "assess", _planner(), _registry(authorization), RunContext(reviewing), gate=gate
    )

    # read_file is permitted for account-review; score_applicant is not.
    assert result.tool_calls[0].tool_name == "read_file"
    assert result.tool_calls[0].error is None
    assert "does not permit" in result.tool_calls[1].error


async def test_the_correct_purpose_completes_but_a_human_signs_the_decision():
    authorization, _, gate = _regulated()
    applying = Principal(id="analyst-7", roles=frozenset({"analyst"}), purpose="credit-application")

    result = await run_agent(
        "assess", _planner(), _registry(authorization), RunContext(applying), gate=gate
    )

    assert result.outcome is RunOutcome.COMPLETED
    approvals = [s for s in result.steps if s.kind is StepKind.APPROVAL]
    # read_file is elevated and below the threshold; score_applicant is not.
    assert len(approvals) == 1
    assert approvals[0].tool_name == "score_applicant"
    assert approvals[0].metadata["approver"] == "supervisor-1"


async def test_the_reviewer_cannot_be_the_requester():
    """Four eyes, with the requester removed from the eligible set."""
    authorization, _, _ = _regulated()
    _, _, lone_gate = regulated_overlay(
        tool_purposes={
            "score_applicant": frozenset({"credit-application"}),
            "read_file": frozenset({"credit-application"}),
        },
        reviewers=frozenset({"analyst-7"}),
        gate_from=RiskTier.CONSEQUENTIAL,
    )
    applying = Principal(id="analyst-7", roles=frozenset({"analyst"}), purpose="credit-application")

    result = await run_agent(
        "assess", _planner(), _registry(authorization), RunContext(applying), gate=lone_gate
    )
    assert result.outcome is RunOutcome.NOT_APPROVED


# ============================================================ the audit trail
async def test_the_regulated_trace_withholds_sensitive_arguments():
    """A deliberate trade: inspectable, and no longer replayable."""
    authorization, audit, gate = _regulated()
    applying = Principal(id="analyst-7", roles=frozenset({"analyst"}), purpose="credit-application")
    registry = _registry(authorization)
    ctx = RunContext(applying, tier=RiskTier.CONSEQUENTIAL)

    result = await run_agent("assess", _planner(), registry, ctx, gate=gate)
    trace = record_trace("assess", ctx, result, registry, audit=audit)

    scoring = [s for s in trace.steps if s.tool_name == "score_applicant"]
    assert all(s.arguments is None for s in scoring)
    assert all(s.metadata.get("arguments_withheld") for s in scoring)

    reading = [s for s in trace.steps if s.tool_name == "read_file"]
    assert any(s.arguments == {"consumer_id": "c-42"} for s in reading)

    assert trace.provenance.authorization_policy == "purpose-based"
    assert trace.provenance.audit_policy == "regulated"


async def test_a_withheld_trace_refuses_to_replay_rather_than_replaying_wrongly():
    authorization, audit, gate = _regulated()
    applying = Principal(id="analyst-7", roles=frozenset({"analyst"}), purpose="credit-application")
    registry = _registry(authorization)
    ctx = RunContext(applying)
    result = await run_agent("assess", _planner(), registry, ctx, gate=gate)
    trace = record_trace("assess", ctx, result, registry, audit=audit)

    try:
        decisions_from(trace)
    except TraceError as exc:
        assert "withheld" in str(exc)
    else:
        raise AssertionError("a withheld trace replayed silently")


async def test_a_neutral_trace_replays_faithfully():
    """The same run, recorded under a policy that keeps its arguments."""
    analyst = Principal(id="analyst-7", roles=frozenset({"analyst"}))
    registry = _registry(RoleBasedAuthorization({n: frozenset({"analyst"}) for n in TOOLS}))
    ctx = RunContext(analyst)

    result = await run_agent(
        "assess", _planner(), registry, ctx, gate=TierGate(AutoApprove(), RiskTier.CRITICAL)
    )
    trace = record_trace("assess", ctx, result, registry, audit=StandardAudit())

    report = await replay(trace)
    assert report.faithful
    assert report.outcome_matches


async def test_replaying_a_permitted_run_under_the_stricter_overlay_diverges():
    """The question behind most replay requests, answered by the harness."""
    analyst = Principal(id="analyst-7", roles=frozenset({"analyst"}))
    permissive = _registry(RoleBasedAuthorization({n: frozenset({"analyst"}) for n in TOOLS}))
    ctx = RunContext(analyst)

    result = await run_agent("assess", _planner(), permissive, ctx)
    trace = record_trace("assess", ctx, result, permissive, audit=StandardAudit())
    assert result.outcome is RunOutcome.COMPLETED

    # Since then, purpose-based authorisation has been adopted.
    authorization, _, _ = _regulated()
    report = await replay(trace, registry=_registry(authorization))

    assert not report.faithful
    assert "DIVERGED" in report.summary()
