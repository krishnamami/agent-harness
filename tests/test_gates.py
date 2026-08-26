"""Approval gates.

The claim: oversight is proportionate, refusals are terminal, and every
approval is in the record.
"""

from __future__ import annotations

import pytest

from harness import (
    ApprovalGate,
    ApprovalRequest,
    AutoApprove,
    CallTool,
    Finish,
    FourEyesGate,
    Principal,
    RecordingGate,
    RefuseAll,
    RiskTier,
    RunContext,
    RunOutcome,
    ScriptedPlanner,
    TierGate,
    ToolRegistry,
    ToolSpec,
    run_agent,
)
from harness.run import StepKind

OBJ = {"type": "object", "properties": {"q": {"type": "string"}}}
P = Principal(id="analyst-7", roles=frozenset({"analyst"}))


async def _ok(arguments):
    return {"ok": True}


def _registry(**tiers):
    r = ToolRegistry()
    for name, tier in tiers.items():
        r.register(ToolSpec(name=name, description=name, parameters=OBJ, tier=tier), _ok)
    return r


def _req(tier=RiskTier.ROUTINE, principal=P):
    return ApprovalRequest(run_id="r", principal=principal, tool="t", arguments={}, tier=tier)


# ----------------------------------------------------------------- protocol
async def test_the_shipped_gates_satisfy_the_protocol():
    assert isinstance(AutoApprove(), ApprovalGate)
    assert isinstance(RefuseAll(), ApprovalGate)
    assert isinstance(TierGate(AutoApprove(), RiskTier.ELEVATED), ApprovalGate)


async def test_auto_approve_is_named_so_nobody_mistakes_it_for_a_control():
    decision = await AutoApprove().review(_req())
    assert decision.approved
    assert AutoApprove().name == "auto-approve"


# -------------------------------------------------------------- proportionate
async def test_a_gate_does_not_engage_below_its_threshold():
    """Routine work is untouched, so the gate keeps its credibility."""
    gate = TierGate(RefuseAll(), minimum_tier=RiskTier.CONSEQUENTIAL)
    decision = await gate.review(_req(RiskTier.ROUTINE))
    assert decision.approved
    assert decision.approver == "not-gated"


async def test_a_gate_engages_at_its_threshold():
    gate = TierGate(RefuseAll(), minimum_tier=RiskTier.CONSEQUENTIAL)
    assert not (await gate.review(_req(RiskTier.CONSEQUENTIAL))).approved


async def test_a_gate_engages_above_its_threshold():
    gate = TierGate(RefuseAll(), minimum_tier=RiskTier.ELEVATED)
    assert not (await gate.review(_req(RiskTier.CRITICAL))).approved


def test_engagement_is_inspectable_without_running_a_review():
    gate = TierGate(AutoApprove(), minimum_tier=RiskTier.ELEVATED)
    assert not gate.engages_for(RiskTier.ROUTINE)
    assert gate.engages_for(RiskTier.CRITICAL)


# ------------------------------------------------------------------ four eyes
async def test_four_eyes_picks_a_reviewer_who_is_not_the_requester():
    gate = FourEyesGate(frozenset({"analyst-7", "supervisor-1"}))
    decision = await gate.review(_req(principal=P))
    assert decision.approved
    assert decision.approver == "supervisor-1"


async def test_four_eyes_refuses_when_the_only_reviewer_is_the_requester():
    """Self-approval is the exact failure a four-eyes control exists to stop."""
    gate = FourEyesGate(frozenset({"analyst-7"}))
    decision = await gate.review(_req(principal=P))
    assert not decision.approved
    assert "requester" in decision.reason


def test_four_eyes_needs_a_reviewer():
    with pytest.raises(ValueError, match="at least one reviewer"):
        FourEyesGate(frozenset())


# -------------------------------------------------------------- in a real run
async def test_a_refusal_stops_the_run_and_is_terminal():
    """An agent that rephrases until someone says yes is worse than no gate."""
    planner = ScriptedPlanner(
        CallTool(tool="payments", arguments={}, rationale="move money"),
        CallTool(tool="payments", arguments={}, rationale="try again"),
        Finish(output="done"),
    )
    result = await run_agent(
        "g",
        planner,
        _registry(payments=RiskTier.CRITICAL),
        RunContext(P),
        gate=TierGate(RefuseAll(), minimum_tier=RiskTier.CONSEQUENTIAL),
    )
    assert result.outcome is RunOutcome.NOT_APPROVED
    assert result.tool_calls == (), "the tool ran despite being refused"


async def test_the_refusal_is_recorded_with_the_approver():
    """An approval that leaves no trace is indistinguishable from none."""
    result = await run_agent(
        "g",
        ScriptedPlanner(CallTool(tool="payments", arguments={})),
        _registry(payments=RiskTier.CRITICAL),
        RunContext(P),
        gate=TierGate(RefuseAll(approver="risk-officer"), RiskTier.ELEVATED),
    )
    approval = next(s for s in result.steps if s.kind is StepKind.APPROVAL)
    assert approval.metadata["approver"] == "risk-officer"
    assert "not approved" in approval.error


async def test_an_approval_is_recorded_too_not_only_a_refusal():
    result = await run_agent(
        "g",
        ScriptedPlanner(CallTool(tool="payments", arguments={}), Finish(output="x")),
        _registry(payments=RiskTier.CRITICAL),
        RunContext(P),
        gate=TierGate(FourEyesGate(frozenset({"supervisor-1"})), RiskTier.ELEVATED),
    )
    assert result.outcome is RunOutcome.COMPLETED
    approvals = [s for s in result.steps if s.kind is StepKind.APPROVAL]
    assert len(approvals) == 1
    assert approvals[0].metadata["approver"] == "supervisor-1"


async def test_ungated_calls_add_no_approval_steps():
    """Otherwise every routine run is padded with noise."""
    result = await run_agent(
        "g",
        ScriptedPlanner(CallTool(tool="search", arguments={}), Finish(output="x")),
        _registry(search=RiskTier.ROUTINE),
        RunContext(P),
        gate=TierGate(AutoApprove(), RiskTier.CRITICAL),
    )
    assert [s for s in result.steps if s.kind is StepKind.APPROVAL] == []


async def test_the_tool_tier_lifts_a_routine_run():
    """A routine run calling a critical tool is a critical call."""
    recorder = RecordingGate(AutoApprove())
    await run_agent(
        "g",
        ScriptedPlanner(CallTool(tool="payments", arguments={}), Finish(output="x")),
        _registry(payments=RiskTier.CRITICAL),
        RunContext(P, tier=RiskTier.ROUTINE),
        gate=TierGate(recorder, RiskTier.CONSEQUENTIAL),
    )
    assert recorder.requests[0].tier is RiskTier.CRITICAL


async def test_the_run_tier_lifts_a_routine_tool():
    recorder = RecordingGate(AutoApprove())
    await run_agent(
        "g",
        ScriptedPlanner(CallTool(tool="search", arguments={}), Finish(output="x")),
        _registry(search=RiskTier.ROUTINE),
        RunContext(P, tier=RiskTier.CRITICAL),
        gate=TierGate(recorder, RiskTier.CONSEQUENTIAL),
    )
    assert recorder.requests[0].tier is RiskTier.CRITICAL


async def test_the_reviewer_sees_the_rationale_and_the_progress():
    """'May this agent call this' is unanswerable without why and what so far."""
    recorder = RecordingGate(AutoApprove())
    await run_agent(
        "review a file",
        ScriptedPlanner(
            CallTool(tool="search", arguments={"q": "x"}),
            CallTool(tool="payments", arguments={"amt": 1}, rationale="settle the balance"),
            Finish(output="x"),
        ),
        _registry(search=RiskTier.ROUTINE, payments=RiskTier.CRITICAL),
        RunContext(P),
        gate=TierGate(recorder, RiskTier.CONSEQUENTIAL),
    )
    request = recorder.requests[0]
    assert request.tool == "payments"
    assert request.rationale == "settle the balance"
    assert request.steps_taken > 0
    assert request.arguments == {"amt": 1}


async def test_a_run_with_no_gate_behaves_as_before():
    """Backwards compatibility: the gate is opt-in."""
    result = await run_agent(
        "g",
        ScriptedPlanner(CallTool(tool="payments", arguments={}), Finish(output="x")),
        _registry(payments=RiskTier.CRITICAL),
        RunContext(P),
    )
    assert result.outcome is RunOutcome.COMPLETED
    assert [s for s in result.steps if s.kind is StepKind.APPROVAL] == []


async def test_an_approval_step_does_not_disturb_the_failure_streak():
    """It is neither an action nor planning."""
    ctx = RunContext(P)
    result = await run_agent(
        "g",
        ScriptedPlanner(CallTool(tool="payments", arguments={}), Finish(output="x")),
        _registry(payments=RiskTier.CRITICAL),
        ctx,
        gate=TierGate(FourEyesGate(frozenset({"sup"})), RiskTier.ELEVATED),
    )
    assert result.outcome is RunOutcome.COMPLETED
    assert ctx.consecutive_failures == 0
