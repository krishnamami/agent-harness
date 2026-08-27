"""Tools the harness did not define, and the tier it must not invent.

The risky part of loading a remote tool surface is not the transport. It is
that the approval gate keys on `ToolSpec.tier`, and the tier now arrives over a
wire the harness does not control. A tool served without readable risk metadata
must not become `ROUTINE` by default, because `ROUTINE` is what the gate waves
through. See ADR-0019.
"""

from __future__ import annotations

import pytest

from harness import (
    AutoApprove,
    CallTool,
    Finish,
    Principal,
    RecordingGate,
    RemoteToolError,
    RiskTier,
    RunContext,
    RunLimits,
    RunOutcome,
    ScriptedPlanner,
    TierGate,
    ToolDeniedError,
    ToolRegistry,
    load_tools,
    run_agent,
)

REFUND_SCHEMA = {
    "type": "object",
    "properties": {"payment_id": {"type": "string"}, "decision_record_id": {"type": "string"}},
    "required": ["payment_id", "decision_record_id"],
    "additionalProperties": False,
}

P = Principal(id="agent", roles=frozenset({"ops"}))
GOOD = {"payment_id": "P0042", "decision_record_id": "DR-1"}


class _Server:
    """Stands in for tool-registry over the wire. Records what it was asked."""

    def __init__(self, tools: list[dict] | None = None, reply: dict | None = None) -> None:
        self.tools = tools if tools is not None else [_tool()]
        self.reply = reply
        self.calls: list[dict] = []

    async def request(self, method: str, params: dict) -> dict:
        if method == "tools/list":
            return {"tools": self.tools}
        self.calls.append(params)
        return self.reply or {
            "content": [{"type": "text", "text": "issue_refund executed on P0042"}],
            "isError": False,
            "structuredContent": {"action_record_id": "AR-1", "state": "executed"},
        }


def _tool(**overrides: object) -> dict:
    tool = {
        "name": "issue_refund",
        "description": "Return captured funds to the customer.",
        "inputSchema": REFUND_SCHEMA,
        "_meta": {"risk_tier": "consequential", "reversible": False},
    }
    tool.update(overrides)
    return tool


async def _load(server: _Server, **kwargs: object) -> ToolRegistry:
    registry = ToolRegistry()
    await load_tools(server, registry, **kwargs)  # type: ignore[arg-type]
    return registry


# ------------------------------------------------------------------ loading


async def test_served_tools_become_registered_tools() -> None:
    registry = await _load(_Server())
    assert registry.names == ("issue_refund",)
    assert registry.spec("issue_refund").parameters == REFUND_SCHEMA


async def test_the_served_schema_is_used_unedited() -> None:
    """Rewriting it locally would mean the harness validated against a contract
    the server does not hold, and the disagreement would surface as an
    unexplained remote refusal."""
    registry = await _load(_Server())
    with pytest.raises(Exception) as caught:
        registry.check("issue_refund", {"payment_id": "P0042"}, P)
    assert "decision_record_id" in str(caught.value)


async def test_the_tier_comes_from_the_server() -> None:
    registry = await _load(_Server())
    assert registry.spec("issue_refund").tier is RiskTier.CONSEQUENTIAL


# --------------------------------------------- the tier it must not invent


async def test_a_tool_with_no_risk_metadata_is_not_routine() -> None:
    """The one that matters. `ROUTINE` is what the gate waves through, so an
    unreadable tier defaulting to it would send a consequential action through
    unreviewed. Unknown is not safe; unknown is unknown."""
    registry = await _load(_Server([_tool(_meta={})]))
    assert registry.spec("issue_refund").tier is RiskTier.CONSEQUENTIAL


async def test_a_tool_with_no_meta_block_at_all_is_not_routine() -> None:
    served = _tool()
    del served["_meta"]
    registry = await _load(_Server([served]))
    assert registry.spec("issue_refund").tier is RiskTier.CONSEQUENTIAL


async def test_an_unrecognised_tier_name_is_not_routine() -> None:
    """A server on a newer vocabulary than this harness. Reading `"severe"` as
    routine would be the harness silently downgrading something it did not
    understand."""
    registry = await _load(_Server([_tool(_meta={"risk_tier": "severe"})]))
    assert registry.spec("issue_refund").tier is RiskTier.CONSEQUENTIAL


async def test_a_non_string_tier_is_not_routine() -> None:
    registry = await _load(_Server([_tool(_meta={"risk_tier": 0})]))
    assert registry.spec("issue_refund").tier is RiskTier.CONSEQUENTIAL


async def test_the_fallback_is_configurable_but_never_implicit() -> None:
    registry = await _load(_Server([_tool(_meta={})]), unknown_tier=RiskTier.CRITICAL)
    assert registry.spec("issue_refund").tier is RiskTier.CRITICAL


async def test_a_declared_routine_tier_is_honoured() -> None:
    """The fallback must not become a floor -- a server that says routine is
    trusted, or tiering stops being proportionate and gets routed around."""
    registry = await _load(_Server([_tool(_meta={"risk_tier": "routine"})]))
    assert registry.spec("issue_refund").tier is RiskTier.ROUTINE


async def test_an_unmetadated_tool_still_reaches_the_gate() -> None:
    """Not just the tier value -- the consequence of it."""
    gate = RecordingGate(AutoApprove())
    registry = await _load(_Server([_tool(_meta={})]))
    planner = ScriptedPlanner(
        CallTool(tool="issue_refund", arguments=GOOD, rationale="refund"),
        Finish(output="done", rationale="done"),
    )
    await run_agent(
        "refund",
        planner,
        registry,
        RunContext(P),
        gate=TierGate(gate, RiskTier.CONSEQUENTIAL),
    )
    assert [r.tool for r in gate.requests] == ["issue_refund"]
    assert gate.requests[0].tier is RiskTier.CONSEQUENTIAL


# ------------------------------------------------------------ what comes back


async def test_a_successful_call_returns_the_action_record() -> None:
    server = _Server()
    registry = await _load(server)
    result = await registry.invoke("issue_refund", GOOD, P)
    assert result == {"action_record_id": "AR-1", "state": "executed"}
    assert server.calls == [{"name": "issue_refund", "arguments": GOOD}]


async def test_a_refusal_becomes_a_denial() -> None:
    """The registry refuses for want of entitlement. The executor already
    treats a denial as an observation the planner can act on, which is exactly
    what "this needs a duplicate_assessment that reached BLOCK" deserves."""
    server = _Server(
        reply={
            "content": [
                {
                    "type": "text",
                    "text": "not authorized: duplicate_assessment=CLEAR does not "
                    "entitle this action (entitled by: duplicate_assessment=BLOCK)",
                }
            ],
            "isError": True,
        }
    )
    registry = await _load(server)
    with pytest.raises(ToolDeniedError) as caught:
        await registry.invoke("issue_refund", GOOD, P)
    assert "duplicate_assessment=BLOCK" in caught.value.reason


async def test_an_action_that_ran_and_failed_is_not_a_denial() -> None:
    """The first question in an incident is whether anything changed out there.
    Flattening both into one exception type destroys the answer."""
    server = _Server(
        reply={
            "content": [{"type": "text", "text": "action failed: processor timeout"}],
            "isError": True,
            "structuredContent": {"action_record_id": "AR-9", "state": "failed"},
        }
    )
    registry = await _load(server)
    with pytest.raises(RemoteToolError) as caught:
        await registry.invoke("issue_refund", GOOD, P)
    assert caught.value.action_record_id == "AR-9"


async def test_a_refusal_with_no_text_still_says_something() -> None:
    registry = await _load(_Server(reply={"isError": True}))
    with pytest.raises(ToolDeniedError) as caught:
        await registry.invoke("issue_refund", GOOD, P)
    assert caught.value.reason == "refused without a reason"


# -------------------------------------------------------------- in the loop


async def test_a_remote_denial_is_fed_back_and_the_run_continues() -> None:
    server = _Server(
        reply={
            "content": [{"type": "text", "text": "not authorized: no decision record 'DR-1'"}],
            "isError": True,
        }
    )
    registry = await _load(server)
    planner = ScriptedPlanner(
        CallTool(tool="issue_refund", arguments=GOOD, rationale="try"),
        Finish(output="escalating instead", rationale="no entitlement"),
    )
    result = await run_agent("refund", planner, registry, RunContext(P))

    assert result.outcome is RunOutcome.COMPLETED
    failed = [s for s in result.steps if s.failed]
    assert "no decision record" in (failed[0].error or "")


async def test_a_server_that_keeps_refusing_hits_the_give_up_ceiling() -> None:
    server = _Server(
        reply={"content": [{"type": "text", "text": "not authorized"}], "isError": True}
    )
    registry = await _load(server)
    call = CallTool(tool="issue_refund", arguments=GOOD, rationale="again")
    planner = ScriptedPlanner(call, call, call, call, call)
    result = await run_agent(
        "refund",
        planner,
        registry,
        RunContext(P, limits=RunLimits(max_consecutive_failures=3)),
    )
    assert result.outcome is RunOutcome.GAVE_UP


async def test_local_rate_limits_are_the_harness_own_budget() -> None:
    """The server's limit is the one that binds. This one exists so a runaway
    loop is stopped in-process rather than by a remote refusal a model has to
    interpret."""
    registry = await _load(_Server(), rate_limits={"issue_refund": 1})
    assert registry.spec("issue_refund").rate_limit_per_minute == 1
