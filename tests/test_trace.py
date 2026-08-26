"""Traces and replay.

The claim being tested: a recorded run can be reconstructed, and if today's
system would not reproduce it, that is reported rather than hidden.
"""

from __future__ import annotations

import json

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
    RunTrace,
    ScriptedPlanner,
    ToolRegistry,
    ToolSpec,
    TraceError,
    record_trace,
    replay,
    run_agent,
)
from harness.policy import StandardAudit
from harness.trace import TRACE_FORMAT_VERSION, decisions_from

OBJ = {"type": "object", "properties": {"q": {"type": "string"}}}
P = Principal(id="analyst-7", roles=frozenset({"analyst"}))


async def _ok(arguments):
    return {"found": arguments.get("q", "")}


async def _boom(arguments):
    raise RuntimeError("downstream is down")


def _registry(authorization=None, **tools):
    r = ToolRegistry(authorization)
    for name, fn in tools.items():
        r.register(ToolSpec(name=name, description=name, parameters=OBJ), fn)
    return r


async def _recorded_run(planner=None, registry=None, ctx=None):
    planner = planner or ScriptedPlanner(
        CallTool(tool="search", arguments={"q": "consumer-42"}, rationale="look it up"),
        CallTool(tool="summarise", arguments={"q": "consumer-42"}, rationale="condense"),
        Finish(output="a summary", rationale="done"),
    )
    registry = registry or _registry(search=_ok, summarise=_ok)
    ctx = ctx or RunContext(P, RunLimits(max_steps=10), tier=RiskTier.CONSEQUENTIAL)
    result = await run_agent("review the file", planner, registry, ctx)
    return record_trace("review the file", ctx, result, registry), result


# ------------------------------------------------------------------ recording
async def test_a_trace_captures_the_run():
    trace, result = await _recorded_run()
    assert trace.run_id == result.run_id
    assert trace.outcome is RunOutcome.COMPLETED
    assert trace.principal_id == "analyst-7"
    assert trace.tier is RiskTier.CONSEQUENTIAL
    assert len(trace.steps) == len(result.steps)


async def test_a_trace_records_the_limits_that_were_in_force():
    """Replaying under today's limits would not be replaying the run."""
    trace, _ = await _recorded_run(ctx=RunContext(P, RunLimits(max_steps=7, max_cost_usd=0.25)))
    assert trace.limits.max_steps == 7
    assert trace.limits.max_cost_usd == 0.25


async def test_provenance_records_the_policy_and_the_tool_contracts():
    """So a divergence can be attributed to the code, the policy, or the tools."""
    trace, _ = await _recorded_run(
        registry=_registry(RoleBasedAuthorization({"search": frozenset({"analyst"})}), search=_ok)
    )
    assert trace.provenance.authorization_policy == "role-based"
    assert "search" in trace.provenance.tool_digests
    assert len(trace.provenance.tool_digests["search"]) == 16


async def test_a_changed_tool_contract_changes_its_digest():
    r1 = ToolRegistry()
    r1.register(ToolSpec(name="search", description="v1", parameters=OBJ), _ok)
    r2 = ToolRegistry()
    r2.register(ToolSpec(name="search", description="v2 — now returns more", parameters=OBJ), _ok)

    t1, _ = await _recorded_run(registry=r1, planner=ScriptedPlanner(Finish(output=None)))
    t2, _ = await _recorded_run(registry=r2, planner=ScriptedPlanner(Finish(output=None)))
    assert t1.provenance.tool_digests["search"] != t2.provenance.tool_digests["search"]


# --------------------------------------------------------------- round trip
async def test_a_trace_survives_json():
    trace, _ = await _recorded_run()
    restored = RunTrace.from_json(trace.to_json())
    assert restored.run_id == trace.run_id
    assert restored.outcome is trace.outcome
    assert len(restored.steps) == len(trace.steps)
    assert restored.steps[1].arguments == trace.steps[1].arguments


async def test_an_unreadable_trace_is_refused_not_guessed():
    """Silently misreading an audit record is worse than not reading it."""
    trace, _ = await _recorded_run()
    data = trace.to_dict()
    data["trace_format"] = TRACE_FORMAT_VERSION + 1
    with pytest.raises(TraceError, match="not readable"):
        RunTrace.from_dict(data)


def test_malformed_json_raises_a_trace_error():
    with pytest.raises(TraceError, match="unreadable"):
        RunTrace.from_json("{not json")


# ------------------------------------------------------------------- replay
async def test_a_faithful_replay_reports_no_divergence():
    trace, _ = await _recorded_run()
    report = await replay(trace)
    assert report.faithful
    assert report.outcome_matches
    assert report.divergences == ()
    assert "faithful" in report.summary()


async def test_replay_reproduces_the_tool_sequence_without_calling_anything():
    called: list[str] = []

    async def live(arguments):
        called.append("live call")
        return {"x": 1}

    trace, _ = await _recorded_run(
        planner=ScriptedPlanner(CallTool(tool="search", arguments={"q": "a"}), Finish(output="d")),
        registry=_registry(search=live),
    )
    called.clear()

    report = await replay(trace)
    assert report.faithful
    assert called == [], "replay reached a live tool"
    assert [s.tool_name for s in report.replayed.tool_calls] == ["search"]


async def test_replay_reproduces_a_recorded_failure():
    """A run that failed must replay as failing, or the record is not the run."""
    trace, original = await _recorded_run(
        planner=ScriptedPlanner(CallTool(tool="flaky", arguments={}), Finish(output="recovered")),
        registry=_registry(flaky=_boom),
    )
    assert any(s.failed for s in original.steps)

    report = await replay(trace)
    assert report.faithful
    assert any(s.failed for s in report.replayed.steps)


async def test_replay_reproduces_a_bounded_run():
    trace, original = await _recorded_run(
        planner=ScriptedPlanner(*[CallTool(tool="search", arguments={}) for _ in range(20)]),
        ctx=RunContext(P, RunLimits(max_steps=5)),
    )
    assert original.outcome is RunOutcome.STEP_LIMIT

    report = await replay(trace)
    assert report.replayed.outcome is RunOutcome.STEP_LIMIT
    assert report.outcome_matches


# --------------------------------------------------------------- divergence
async def test_replay_under_a_stricter_policy_diverges_and_says_so():
    """The question behind most replay requests: would this still be permitted?"""
    trace, _ = await _recorded_run(
        planner=ScriptedPlanner(CallTool(tool="payments", arguments={}), Finish(output="paid")),
        registry=_registry(payments=_ok),
    )

    # The analyst has since lost access to payments.
    today = _registry(RoleBasedAuthorization({"payments": frozenset({"treasury"})}), payments=_ok)
    report = await replay(trace, registry=today)

    assert not report.faithful
    assert "DIVERGED" in report.summary()
    assert any(d.field in ("error", "outcome", "step_count") for d in report.divergences)


async def test_divergence_names_the_step_and_the_field():
    trace, _ = await _recorded_run()
    tampered = RunTrace.from_dict(
        {
            **trace.to_dict(),
            "outcome": str(RunOutcome.GAVE_UP),
        }
    )
    report = await replay(tampered)
    assert not report.faithful
    assert any(d.field == "outcome" for d in report.divergences)


# ----------------------------------------------------------------- redaction
async def test_withheld_arguments_make_a_trace_inspectable_but_not_replayable():
    """A deliberate trade, and it must fail loudly rather than replay wrongly."""

    class Minimal:
        name = "minimal"

        def retention_days(self, tier):
            return 30

        def must_record_arguments(self, tool_name):
            return False

    planner = ScriptedPlanner(CallTool(tool="search", arguments={"q": "ssn"}), Finish(output="x"))
    registry = _registry(search=_ok)
    ctx = RunContext(P)
    result = await run_agent("g", planner, registry, ctx)
    trace = record_trace("g", ctx, result, registry, audit=Minimal())

    call = next(s for s in trace.steps if s.tool_name == "search")
    assert call.arguments is None
    assert call.metadata["arguments_withheld"] is True

    with pytest.raises(TraceError, match="withheld"):
        decisions_from(trace)


async def test_the_default_audit_policy_records_arguments():
    trace, _ = await _recorded_run()
    assert trace.provenance.audit_policy == StandardAudit().name
    call = next(s for s in trace.steps if s.tool_name)
    assert call.arguments is not None


# -------------------------------------------------------------- reconstruction
async def test_decisions_are_rebuilt_from_the_step_record():
    trace, _ = await _recorded_run()
    decisions = decisions_from(trace)
    assert [type(d).__name__ for d in decisions] == ["CallTool", "CallTool", "Finish"]
    assert decisions[0].tool == "search"
    assert decisions[0].rationale == "look it up"


async def test_a_trace_is_json_serialisable_end_to_end():
    trace, _ = await _recorded_run()
    raw = trace.to_json()
    json.loads(raw)  # must not raise
    assert '"run_id"' in raw
    assert '"provenance"' in raw
