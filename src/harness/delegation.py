"""Delegation.

One agent handing a sub-goal to another, without handing over its authority.

The obvious implementation is a function that starts a second run and returns
its result. That version works on the first day and is a privilege-escalation
path by the second, because nothing stops the child asking for more than the
parent holds -- more roles, more budget, more time, or another delegation ten
levels down.

So the primitive is defined by its invariants rather than its signature:

- **A principal may only narrow.** A child's roles must be a subset of its
  parent's, and it may not change a purpose the parent already declared.
  Escalation by delegation is the obvious attack on any agent tree, and it is
  refused structurally rather than reviewed for.

- **Cost and time are drawn, not granted.** They are fungible: a dollar the
  child spends is a dollar the parent no longer has. A child's ceilings are
  capped at whatever the parent has left, so total spend across a tree of any
  shape stays bounded by the root.

- **Steps are not drawn.** They are structural rather than fungible -- a step
  is one turn of one loop, and a child's twenty turns are not the parent's.
  What bounds the tree is depth, plus the cost and time that *are* drawn.

- **A delegation is one step in the parent's record.** Not the child's twenty.
  Otherwise a single failed sub-run would register as twenty consecutive
  failures and trip the give-up ceiling from ADR-0005 on its own.

Two entry points, because a tree needs both:

`new_child_context` is the invariant-enforcing constructor. It is what a
service calls when the sub-agent is itself something that delegates further --
a coordinator handing to an investigator that hands to an extractor. Without a
way to obtain the child's context, nothing below depth one could ever delegate,
and the depth ceiling would be enforced by accident rather than by design.

`delegate` is the common case: build the child, run it, record it.

Two failure modes are deliberately handled differently. A child asking for
authority the parent lacks is a *bug*, and raises. A child that cannot be
afforded is a *runtime condition*, and is reported as a named outcome -- the
same way every other ceiling in this harness reports rather than throws.
"""

from __future__ import annotations

import logging
from typing import Any

from harness.executor import run_agent
from harness.gates import ApprovalGate
from harness.planner import Planner
from harness.policy import Principal, RiskTier
from harness.run import (
    RunContext,
    RunLimits,
    RunOutcome,
    RunResult,
    StepKind,
    StepRecord,
)
from harness.spans import DELEGATE, record_error, tracer
from harness.tools import ToolRegistry

logger = logging.getLogger(__name__)


class PrivilegeEscalationError(Exception):
    """A sub-agent asked for authority its parent does not hold.

    Raised rather than returned. Every other bound in this harness reports a
    named outcome, because running out of budget is an ordinary thing that
    happens to correct code. This is not that: a delegation that widens
    authority is a defect in the calling service, and reporting it quietly as
    a failed run would let it ship.
    """


def _why_not_narrower(parent: Principal, child: Principal) -> str | None:
    """Explain why `child` is not a narrowing of `parent`, or None if it is."""
    extra = child.roles - parent.roles
    if extra:
        return f"roles the parent does not hold: {', '.join(sorted(extra))}"

    # A purpose may be *added* where the parent declared none -- under
    # permissible-purpose authorisation a principal with no declared purpose is
    # denied, so declaring one narrows. Changing one already declared does not.
    if parent.purpose is not None and child.purpose != parent.purpose:
        return f"purpose changed from {parent.purpose!r} to {child.purpose!r}"

    for key, value in child.attributes.items():
        if key not in parent.attributes:
            return f"attribute {key!r} is not held by the parent"
        if parent.attributes[key] != value:
            return f"attribute {key!r} differs from the parent's"

    return None


def new_child_context(
    parent: RunContext,
    *,
    principal: Principal | None = None,
    limits: RunLimits | None = None,
    tier: RiskTier | None = None,
    metadata: dict[str, Any] | None = None,
) -> RunContext | RunOutcome:
    """Build a child context under narrowed authority and drawn budget.

    Returns the context, or the `RunOutcome` explaining why no child could be
    started. Raises `PrivilegeEscalationError` if `principal` widens authority
    rather than narrowing it.
    """
    child_principal = principal if principal is not None else parent.principal

    why = _why_not_narrower(parent.principal, child_principal)
    if why is not None:
        raise PrivilegeEscalationError(
            f"{child_principal.id} cannot be delegated to by {parent.principal.id}: {why}"
        )

    depth = parent.depth + 1
    if depth > parent.limits.max_delegation_depth:
        return RunOutcome.DEPTH_LIMIT

    # The parent needs a step of its own to record the delegation in.
    if parent.step_count >= parent.limits.max_steps:
        return RunOutcome.STEP_LIMIT
    if parent.remaining_usd <= 0:
        return RunOutcome.COST_LIMIT
    if parent.remaining_seconds <= 0:
        return RunOutcome.TIME_LIMIT

    req = limits if limits is not None else parent.limits
    drawn = RunLimits(
        # Structural, and the child's own. See the module docstring.
        max_steps=req.max_steps,
        max_consecutive_failures=req.max_consecutive_failures,
        default_tool_timeout_seconds=req.default_tool_timeout_seconds,
        # Fungible, and the parent's, lent out.
        max_cost_usd=min(req.max_cost_usd, parent.remaining_usd),
        max_wall_clock_seconds=min(req.max_wall_clock_seconds, parent.remaining_seconds),
        # Belongs to the tree, not to any run in it. Taken from the parent
        # rather than from `req`, so a branch cannot buy itself more room by
        # asking for it on the way down.
        max_delegation_depth=parent.limits.max_delegation_depth,
    )

    return RunContext(
        principal=child_principal,
        limits=drawn,
        tier=tier if tier is not None else parent.tier,
        depth=depth,
        metadata={
            **(metadata or {}),
            "parent_run_id": parent.run_id,
            # The parent step this delegation hangs off. Without it a tree of
            # traces is a set of runs that happen to share a timestamp.
            "parent_step_index": parent.step_count,
        },
    )


def _refused(
    parent: RunContext,
    sub_goal: str,
    outcome: RunOutcome,
    reason: str,
) -> RunResult:
    """Record a delegation that never started, and report it as a run."""
    parent.record(
        StepRecord(
            index=parent.step_count,
            kind=StepKind.DELEGATION,
            summary=f"delegation refused: {sub_goal}",
            error=reason,
            metadata={"outcome": str(outcome), "depth": parent.depth + 1},
        )
    )
    logger.warning(
        "delegation refused",
        extra={"run_id": parent.run_id, "outcome": str(outcome), "reason": reason},
    )
    return RunResult(run_id=f"{parent.run_id}/refused", outcome=outcome, error=reason)


async def delegate(
    parent: RunContext,
    sub_goal: str,
    planner: Planner,
    registry: ToolRegistry,
    *,
    principal: Principal | None = None,
    limits: RunLimits | None = None,
    gate: ApprovalGate | None = None,
    tier: RiskTier | None = None,
    metadata: dict[str, Any] | None = None,
) -> RunResult:
    """Run `sub_goal` as a child of `parent`, under narrowed authority.

    The child gets its own context, its own registry and its own gate. What it
    does not get is more authority, more money or more time than the parent had
    to give.
    """
    # The span opens before the checks, so a delegation that was *refused* is
    # visible in a backend too. A tree showing only the delegations that
    # happened cannot answer why a branch is missing.
    with tracer.start_as_current_span(DELEGATE) as span:
        span.set_attribute("harness.parent_run_id", parent.run_id)
        span.set_attribute("harness.depth", parent.depth + 1)

        child = new_child_context(
            parent, principal=principal, limits=limits, tier=tier, metadata=metadata
        )
        if isinstance(child, RunOutcome):
            record_error(span, "refused", str(child))
            return _refused(parent, sub_goal, child, f"parent cannot start a child run ({child})")

        span.set_attribute("harness.child.run_id", child.run_id)
        span.set_attribute("harness.child.principal", child.principal.id)
        span.set_attribute("harness.child.budget_usd", round(child.limits.max_cost_usd, 6))

        logger.info(
            "delegating",
            extra={
                "run_id": parent.run_id,
                "child_run_id": child.run_id,
                "depth": child.depth,
                "principal": child.principal.id,
                "budget_usd": round(child.limits.max_cost_usd, 4),
            },
        )

        # The child's own run span nests inside this one, so the agent tree and
        # the span tree are the same shape.
        result = await run_agent(sub_goal, planner, registry, child, gate)
        span.set_attribute("harness.child.outcome", str(result.outcome))

    parent.sub_runs.append(result)

    # One step, carrying the child's whole cost. Recording it through the
    # parent's own `record` is what charges the parent: a delegation the parent
    # did not pay for would let a tree spend its way around the root's ceiling.
    parent.record(
        StepRecord(
            index=parent.step_count,
            kind=StepKind.DELEGATION,
            summary=f"delegated: {sub_goal}",
            result=result.output,
            cost_usd=result.cost_usd,
            # A sub-run that hit a ceiling or was refused is a failed action
            # from the parent's side -- one failure, not one per child step.
            # The parent may still route around it, which is exactly the
            # difference between a refusal being terminal *there* and terminal
            # everywhere. See ADR-0012.
            error=None if result.succeeded else f"sub-run {result.outcome}: {result.error}",
            metadata={
                "child_run_id": result.run_id,
                "outcome": str(result.outcome),
                "child_steps": len(result.steps),
                "depth": child.depth,
                "principal": child.principal.id,
            },
        )
    )

    return result
