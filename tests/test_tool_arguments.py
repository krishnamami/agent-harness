"""A tool's schema is a contract, and a contract nobody checks is a comment.

Every tool has always declared a JSON Schema, and the registry has always said
in its own docstring that the schema exists "so the harness can reject a
malformed call before it reaches a real system". It did not. `check()`
authorised the caller and returned the spec without ever looking at the
arguments, so a refund tool declaring `amount: integer` would happily execute
for `"five"`, and an undeclared `drop_tables: true` rode along untouched.

It stayed invisible for one reason: every test and every README example drives
`ScriptedPlanner`, where a human writes the arguments and naturally writes them
correctly. The hole only opens when a model produces them, which is the entire
point of the harness. See ADR-0018.
"""

from __future__ import annotations

import pytest

from harness import (
    AutoApprove,
    CallTool,
    Finish,
    Principal,
    RecordingGate,
    RiskTier,
    RunContext,
    RunLimits,
    RunOutcome,
    ScriptedPlanner,
    ToolArgumentError,
    ToolRegistry,
    ToolSpec,
    run_agent,
)

REFUND = {
    "type": "object",
    "properties": {
        "amount": {"type": "integer"},
        "payment_id": {"type": "string"},
        "reason": {"type": "string", "enum": ["duplicate", "dispute"]},
    },
    "required": ["amount", "payment_id"],
    "additionalProperties": False,
}

P = Principal(id="agent", roles=frozenset({"ops"}))


class _Recorded:
    """A tool that remembers whether it was reached at all."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(self, arguments: dict[str, object]) -> str:
        self.calls.append(arguments)
        return "refunded"


def _registry(fn: object = None, *, tier: RiskTier = RiskTier.ROUTINE) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="issue_refund", description="refund a payment", parameters=REFUND, tier=tier),
        fn or _Recorded(),
    )
    return registry


# ------------------------------------------------------- the defect itself


def test_a_refund_for_five_dollars_is_not_a_refund_for_five() -> None:
    """The headline case. This executed, and returned "refunded five"."""
    with pytest.raises(ToolArgumentError):
        _registry().check("issue_refund", {"amount": "five", "payment_id": "P1"}, P)


def test_a_missing_required_argument_is_refused() -> None:
    with pytest.raises(ToolArgumentError):
        _registry().check("issue_refund", {"amount": 500}, P)


def test_an_undeclared_argument_is_refused() -> None:
    """`additionalProperties: false` is the tool's business, and the registry
    is the thing that has to honour it."""
    with pytest.raises(ToolArgumentError):
        _registry().check(
            "issue_refund",
            {"amount": 500, "payment_id": "P1", "drop_tables": True},
            P,
        )


def test_a_value_outside_the_declared_enum_is_refused() -> None:
    with pytest.raises(ToolArgumentError):
        _registry().check(
            "issue_refund",
            {"amount": 500, "payment_id": "P1", "reason": "because"},
            P,
        )


def test_well_formed_arguments_still_pass() -> None:
    spec = _registry().check("issue_refund", {"amount": 500, "payment_id": "P1"}, P)
    assert spec.name == "issue_refund"


@pytest.mark.asyncio
async def test_a_malformed_call_never_reaches_the_tool() -> None:
    fn = _Recorded()
    registry = _registry(fn)
    with pytest.raises(ToolArgumentError):
        await registry.invoke("issue_refund", {"amount": "five", "payment_id": "P1"}, P)
    assert fn.calls == []


# ------------------------------------------- what the planner is told back


def test_every_violation_is_reported_not_only_the_first() -> None:
    """A planner told about one missing field at a time spends a model call
    per field. It should learn everything wrong with the call at once."""
    with pytest.raises(ToolArgumentError) as caught:
        _registry().check("issue_refund", {}, P)
    assert len(caught.value.problems) == 2


def test_violations_are_ordered_deterministically() -> None:
    """Two runs of the same bad call must read identically, or a replay diff
    reports a change that did not happen."""
    first = second = ()
    for _ in range(2):
        with pytest.raises(ToolArgumentError) as caught:
            _registry().check("issue_refund", {}, P)
        first, second = second, caught.value.problems
    assert first == second


def test_a_violation_names_the_argument_it_is_about() -> None:
    """ "is not of type 'integer'" is not actionable on its own."""
    with pytest.raises(ToolArgumentError) as caught:
        _registry().check("issue_refund", {"amount": "five", "payment_id": "P1"}, P)
    assert caught.value.problems[0].startswith("amount: ")


def test_a_whole_object_violation_is_labelled_root() -> None:
    with pytest.raises(ToolArgumentError) as caught:
        _registry().check("issue_refund", {"amount": 1}, P)
    assert caught.value.problems[0].startswith("<root>: ")


def test_a_flood_of_violations_is_capped() -> None:
    """The message goes into a prompt. It has to stay bounded."""
    wide = {
        "type": "object",
        "properties": {f"f{i}": {"type": "integer"} for i in range(20)},
        "required": [f"f{i}" for i in range(20)],
    }
    registry = ToolRegistry()
    registry.register(ToolSpec(name="wide", description="d", parameters=wide), _Recorded())
    with pytest.raises(ToolArgumentError) as caught:
        registry.check("wide", {}, P)
    assert len(caught.value.problems) == 20
    assert "and 15 more" in str(caught.value)


# ------------------------------------------------------------- the ordering


def test_a_policy_is_never_asked_to_judge_malformed_arguments() -> None:
    """The reason validation precedes authorisation.

    A policy of the ordinary shape -- "refunds over five hundred need a second
    approver" -- reading `arguments.get("amount", 0) > 500` returns False for a
    missing or string amount, and *permits* the call. Silent permission, not a
    crash, which is the worst of both.
    """

    class _Spy:
        def __init__(self) -> None:
            self.seen: list[dict[str, object]] = []

        def authorize(self, principal, tool_name, arguments):  # type: ignore[no-untyped-def]
            from harness import AuthorizationDecision

            self.seen.append(dict(arguments))
            return AuthorizationDecision(allowed=True)

    spy = _Spy()
    registry = ToolRegistry(authorization=spy)
    registry.register(
        ToolSpec(name="issue_refund", description="d", parameters=REFUND), _Recorded()
    )
    with pytest.raises(ToolArgumentError):
        registry.check("issue_refund", {"amount": "five", "payment_id": "P1"}, P)
    assert spy.seen == []


# ------------------------------------------------ a broken schema is a defect


def test_a_schema_that_is_itself_invalid_fails_at_definition() -> None:
    """A tool with a broken schema is broken where it is written, not on the
    first request that happens to exercise it."""
    with pytest.raises(ValueError, match="invalid JSON Schema"):
        ToolSpec(
            name="bad",
            description="d",
            parameters={"type": "object", "properties": {"a": {"type": "not-a-type"}}},
        )


# --------------------------------------------------------- through the loop


@pytest.mark.asyncio
async def test_the_planner_can_correct_itself_from_the_error() -> None:
    """The point of an observation rather than a crash: the violations reach
    the planner, and a second attempt gets through."""
    fn = _Recorded()
    planner = ScriptedPlanner(
        CallTool(
            tool="issue_refund",
            arguments={"amount": "five", "payment_id": "P1"},
            rationale="first try",
        ),
        CallTool(
            tool="issue_refund",
            arguments={"amount": 5, "payment_id": "P1"},
            rationale="the amount should be a number",
        ),
        Finish(output="refunded", rationale="done"),
    )
    result = await run_agent("refund P1", planner, _registry(fn), RunContext(P))

    assert result.outcome is RunOutcome.COMPLETED
    assert fn.calls == [{"amount": 5, "payment_id": "P1"}]
    failed = [s for s in result.steps if s.failed]
    assert len(failed) == 1
    assert "invalid arguments" in (failed[0].error or "")
    assert "amount" in (failed[0].error or "")


@pytest.mark.asyncio
async def test_a_planner_that_never_corrects_itself_gives_up() -> None:
    """Self-correction without a ceiling is failing slowly and expensively."""
    bad = CallTool(
        tool="issue_refund", arguments={"amount": "five", "payment_id": "P1"}, rationale="again"
    )
    planner = ScriptedPlanner(bad, bad, bad, bad, bad)
    result = await run_agent(
        "refund P1",
        planner,
        _registry(),
        RunContext(P, limits=RunLimits(max_consecutive_failures=3)),
    )
    assert result.outcome is RunOutcome.GAVE_UP


@pytest.mark.asyncio
async def test_a_malformed_call_never_reaches_a_human() -> None:
    """An approver asked to sign off a call that cannot succeed is an approver
    being trained that approvals are inconsequential."""
    gate = RecordingGate(AutoApprove())
    planner = ScriptedPlanner(
        CallTool(
            tool="issue_refund", arguments={"amount": "five", "payment_id": "P1"}, rationale="try"
        ),
        Finish(output=None, rationale="give up"),
    )
    await run_agent(
        "refund P1",
        planner,
        _registry(tier=RiskTier.CONSEQUENTIAL),
        RunContext(P),
        gate=gate,
    )
    assert gate.requests == []
