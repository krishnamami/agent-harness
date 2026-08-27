"""The executor side of the split.

Runs the loop, enforces every bound, and records what happened. The planner
proposes; this decides whether the proposal is allowed to happen.

Nothing here calls a model. Nothing in the planner calls a tool. That is the
whole architecture.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any

from opentelemetry.trace import Span

from harness.gates import ApprovalGate, ApprovalRequest, AutoApprove
from harness.planner import CallTool, CallTools, Decision, Finish, Planner, PlannerState
from harness.run import (
    CostLimitExceededError,
    RunContext,
    RunOutcome,
    RunResult,
    StepKind,
    StepLimitExceededError,
    StepRecord,
    WallClockExceededError,
)
from harness.spans import (
    APPROVAL,
    PLAN,
    RUN,
    TOOL,
    describe_run,
    record_error,
    record_outcome,
    tracer,
)
from harness.tools import (
    RateLimitExceededError,
    ToolArgumentError,
    ToolDeniedError,
    ToolNotFoundError,
    ToolRegistry,
)

logger = logging.getLogger(__name__)


def _result(
    ctx: RunContext,
    outcome: RunOutcome,
    output: Any = None,
    error: str | None = None,
) -> RunResult:
    return RunResult(
        run_id=ctx.run_id,
        outcome=outcome,
        output=output,
        steps=tuple(ctx.steps),
        cost_usd=ctx.spent_usd,
        error=error,
    )


async def run_agent(
    goal: str,
    planner: Planner,
    registry: ToolRegistry,
    ctx: RunContext,
    gate: ApprovalGate | None = None,
) -> RunResult:
    """Run until the agent finishes or a bound stops it.

    Every exit is a named outcome. A run that hits a ceiling is not a failed
    run — `STEP_LIMIT`, `COST_LIMIT`, `TIME_LIMIT` and `GAVE_UP` are the system
    working, and counting them separately from `FAILED` is what lets you tune
    the ceilings instead of guessing at them.
    """
    gate = gate if gate is not None else AutoApprove()

    # The span wraps the whole run, so a delegated child's span nests inside
    # its parent's and the tree an operator sees matches the tree that ran.
    with tracer.start_as_current_span(RUN) as span:
        describe_run(span, ctx)
        logger.info(
            "run started",
            extra={
                "run_id": ctx.run_id,
                "goal": goal,
                "tier": int(ctx.tier),
                "planner": planner.name,
                "gate": gate.name,
            },
        )
        result = await _loop(goal, planner, registry, ctx, gate)
        record_outcome(span, result)
        return result


async def _loop(
    goal: str,
    planner: Planner,
    registry: ToolRegistry,
    ctx: RunContext,
    gate: ApprovalGate,
) -> RunResult:
    """The loop itself. Separated so the run span wraps every exit from it."""
    while True:
        # --- can we afford another step at all? -------------------------
        try:
            ctx.before_step()
        except WallClockExceededError as exc:
            logger.warning("run hit the wall clock", extra={"run_id": ctx.run_id})
            return _result(ctx, RunOutcome.TIME_LIMIT, error=str(exc))
        except StepLimitExceededError as exc:
            logger.warning("run hit the step ceiling", extra={"run_id": ctx.run_id})
            return _result(ctx, RunOutcome.STEP_LIMIT, error=str(exc))
        except CostLimitExceededError as exc:
            logger.warning("run hit the cost ceiling", extra={"run_id": ctx.run_id})
            return _result(ctx, RunOutcome.COST_LIMIT, error=str(exc))

        # --- ask the planner what to do next ----------------------------
        state = PlannerState(
            goal=goal,
            # Only the tools this principal may actually call. A planner shown
            # a tool it cannot use will plan to use it and burn a step
            # learning what the registry already knew.
            tools=registry.describe_for(ctx.principal),
            history=tuple(ctx.steps),
            remaining_steps=ctx.limits.max_steps - ctx.step_count,
            remaining_usd=ctx.remaining_usd,
        )

        started = time.perf_counter()
        with tracer.start_as_current_span(PLAN) as plan_span:
            plan_span.set_attribute("harness.planner", planner.name)
            decision = await _decide(planner, state, ctx, plan_span, started)
        if isinstance(decision, RunResult):
            return decision
        plan_ms = (time.perf_counter() - started) * 1000

        # --- finish -----------------------------------------------------
        if isinstance(decision, Finish):
            ctx.record(
                StepRecord(
                    index=ctx.step_count,
                    kind=StepKind.FINISH,
                    summary=decision.rationale or "finished",
                    result=decision.output,
                    cost_usd=decision.cost_usd,
                    duration_ms=plan_ms,
                )
            )
            logger.info(
                "run completed",
                extra={
                    "run_id": ctx.run_id,
                    "steps": ctx.step_count,
                    "cost_usd": round(ctx.spent_usd, 4),
                },
            )
            return _result(ctx, RunOutcome.COMPLETED, output=decision.output)

        # --- one or more tool calls -------------------------------------
        # Branched on `isinstance` rather than on a boolean flag, so the union
        # is narrowed in both arms and a `CallTools` can never be read as if it
        # had a single `.tool`.
        if isinstance(decision, CallTools):
            calls: tuple[CallTool, ...] = decision.calls
            # The batch is the intent, so the batch is what is recorded. A PLAN
            # step per call would say the planner took several turns, and a
            # replay built from that record would take several turns too.
            ctx.record(
                StepRecord(
                    index=ctx.step_count,
                    kind=StepKind.PLAN,
                    summary=decision.rationale or f"call {len(calls)} tools in parallel",
                    cost_usd=decision.cost_usd,
                    duration_ms=plan_ms,
                    metadata={
                        "parallel": True,
                        "calls": [
                            {
                                "tool": c.tool,
                                "arguments": c.arguments,
                                "rationale": c.rationale,
                                "estimated_cost_usd": c.estimated_cost_usd,
                            }
                            for c in calls
                        ],
                    },
                )
            )
        else:
            calls = (decision,)
            ctx.record(
                StepRecord(
                    index=ctx.step_count,
                    kind=StepKind.PLAN,
                    summary=decision.rationale or f"call {decision.tool}",
                    # The intent is recorded here, before the ceilings are
                    # checked. A decision formed and then prevented is the thing
                    # an auditor asks about, and recording it only on the
                    # TOOL_CALL step means a run stopped by a ceiling loses its
                    # final intention.
                    tool_name=decision.tool,
                    arguments=decision.arguments,
                    cost_usd=decision.cost_usd,
                    duration_ms=plan_ms,
                )
            )

        # --- is the fan-out itself allowed? -----------------------------
        if len(calls) > ctx.limits.max_parallel_calls:
            # Not a failed call: a plan the run will not carry out. Recorded as
            # a correction so the planner can split it, and counted as a failure
            # so one that keeps asking runs out of patience rather than turns.
            ctx.record(
                StepRecord(
                    index=ctx.step_count,
                    kind=StepKind.CORRECTION,
                    summary=f"refused a batch of {len(calls)}",
                    error=(
                        f"{len(calls)} parallel calls exceeds "
                        f"max_parallel_calls={ctx.limits.max_parallel_calls}"
                    ),
                )
            )
            if ctx.should_give_up():
                return _result(
                    ctx,
                    RunOutcome.GAVE_UP,
                    error=f"{ctx.consecutive_failures} consecutive failures",
                )
            continue

        # --- is there room to record all of them? -----------------------
        # Checked for the whole batch. Running half of something planned as a
        # unit leaves the planner reasoning about a state that never existed.
        if ctx.step_count + len(calls) > ctx.limits.max_steps:
            return _result(
                ctx,
                RunOutcome.STEP_LIMIT,
                error=f"a batch of {len(calls)} does not fit in the remaining steps",
            )

        # --- can the whole batch be afforded? ---------------------------
        # The sum, not each call in turn. Cost cannot be un-spent, so a batch
        # that would breach the ceiling is refused entire rather than executed
        # up to the line and abandoned.
        projected = sum(c.estimated_cost_usd for c in calls)
        try:
            ctx.before_step(projected_cost_usd=projected)
        except WallClockExceededError as exc:
            return _result(ctx, RunOutcome.TIME_LIMIT, error=str(exc))
        except StepLimitExceededError as exc:
            return _result(ctx, RunOutcome.STEP_LIMIT, error=str(exc))
        except CostLimitExceededError as exc:
            logger.warning(
                "refused a batch that would breach the cost ceiling",
                extra={"run_id": ctx.run_id, "calls": len(calls)},
            )
            return _result(ctx, RunOutcome.COST_LIMIT, error=str(exc))

        # --- would policy permit these at all? --------------------------
        # Checked before the gate, deliberately. Asking a reviewer to approve a
        # call that authorisation will refuse anyway wastes the scarcest thing
        # in the loop, and trains reviewers that approvals are inconsequential.
        #
        # Per call rather than per batch, unlike cost: a denial spends nothing
        # and tells the planner something useful, so the calls that *are*
        # permitted still run and the planner learns both things in one turn.
        permitted: list[CallTool] = []
        for call in calls:
            try:
                registry.check(call.tool, call.arguments, ctx.principal)
            # Rate limits are no longer pre-flighted here: they are consumed
            # at call time so a dry run cannot spend them (ADR-0017). The cost
            # is that a rate-refused call reaches approval before failing.
            except (ToolArgumentError, ToolDeniedError, ToolNotFoundError) as exc:
                # A malformed call is filtered here for the same reason a
                # denied one is: it cannot succeed, so a human should not be
                # asked to approve it. Unlike a denial it is usually the
                # planner's own mistake, so the specific violations go back in
                # the record -- that text is the whole of the correction
                # signal. The step is recorded as a failed TOOL_CALL, so three
                # malformed calls in a row reach the give-up ceiling instead of
                # looping until the step budget runs out.
                ctx.record(
                    StepRecord(
                        index=ctx.step_count,
                        kind=StepKind.TOOL_CALL,
                        summary=(
                            f"{call.tool} rejected before review"
                            if isinstance(exc, ToolArgumentError)
                            else f"{call.tool} refused before review"
                        ),
                        tool_name=call.tool,
                        arguments=call.arguments,
                        error=(
                            f"invalid arguments: {'; '.join(exc.problems)}"
                            if isinstance(exc, ToolArgumentError)
                            else f"denied: {exc.reason}"
                            if isinstance(exc, ToolDeniedError)
                            else f"{type(exc).__name__}: {exc}"
                        ),
                    )
                )
                continue
            permitted.append(call)

        if not permitted:
            if ctx.should_give_up():
                return _result(
                    ctx,
                    RunOutcome.GAVE_UP,
                    error=f"{ctx.consecutive_failures} consecutive failures",
                )
            continue

        # --- does a human need to see any of these? ---------------------
        refusal = await _review(permitted, registry, gate, ctx)
        if refusal is not None:
            return refusal

        # --- run them ---------------------------------------------------
        if len(permitted) > 1:
            records = list(await asyncio.gather(*(_run_one(c, registry, ctx) for c in permitted)))
        else:
            records = [await _run_one(permitted[0], registry, ctx)]

        # `gather` preserves the order of its arguments, not the order things
        # finished -- which is the reason it is used here rather than
        # `as_completed`. Completion order is a property of the network on the
        # day, and a trace that reflected it would not replay to the same thing
        # twice.
        for record in records:
            ctx.record(replace(record, index=ctx.step_count))

        # --- give up? ---------------------------------------------------
        if ctx.should_give_up():
            logger.warning(
                "run gave up after repeated failures",
                extra={"run_id": ctx.run_id, "consecutive_failures": ctx.consecutive_failures},
            )
            return _result(
                ctx,
                RunOutcome.GAVE_UP,
                error=f"{ctx.consecutive_failures} consecutive failures",
            )


async def _decide(
    planner: Planner,
    state: PlannerState,
    ctx: RunContext,
    span: Span,
    started: float,
) -> Decision | RunResult:
    """Ask the planner what to do next.

    Returns the decision, or a `RunResult` if the planner ended the run by
    hanging or raising. Extracted from the loop so the plan span closes on
    every path out of it, including the two that end the run.
    """
    try:
        decision = await asyncio.wait_for(planner.decide(state), ctx.remaining_seconds)
    except TimeoutError:
        # The likeliest hang in the loop: a planner is usually a model call
        # over a network. Bounded by what the run has left rather than by a
        # timeout of its own, so a slow planner cannot carry the run past its
        # ceiling one plan at a time.
        record_error(span, "timeout", "planner timed out")
        logger.warning("planner timed out", extra={"run_id": ctx.run_id})
        ctx.record(
            StepRecord(
                index=ctx.step_count,
                kind=StepKind.PLAN,
                summary="planner timed out",
                error=f"planner exceeded the run's {ctx.limits.max_wall_clock_seconds:.0f}s",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return _result(ctx, RunOutcome.TIME_LIMIT, error="planner timed out")
    except Exception as exc:
        # A planner that raises ends the run. There is no sensible way to
        # continue without knowing what to do next, and retrying a planner that
        # just crashed is how you spend a budget on stack traces.
        record_error(span, "failed", f"{type(exc).__name__}: {exc}")
        logger.exception("planner failed", extra={"run_id": ctx.run_id})
        ctx.record(
            StepRecord(
                index=ctx.step_count,
                kind=StepKind.PLAN,
                summary="planner raised",
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return _result(ctx, RunOutcome.FAILED, error=f"planner: {type(exc).__name__}: {exc}")

    if isinstance(decision, CallTools):
        span.set_attribute("harness.decision", "call_tools")
        span.set_attribute("harness.calls", len(decision.calls))
    elif isinstance(decision, CallTool):
        span.set_attribute("harness.decision", "call_tool")
        span.set_attribute("harness.calls", 1)
    else:
        span.set_attribute("harness.decision", "finish")
    return decision


async def _review(
    calls: list[CallTool],
    registry: ToolRegistry,
    gate: ApprovalGate,
    ctx: RunContext,
) -> RunResult | None:
    """Put each call to the gate. Returns a result only if the run must end.

    A refusal anywhere in a batch stops the batch. ADR-0008 makes a refusal
    terminal for the run, and "terminal except for the other four calls the
    planner asked for in the same breath" is not terminal.
    """
    for call in calls:
        # The effective tier is the higher of the run's and the tool's: a
        # routine run calling a critical tool is a critical call.
        effective_tier = ctx.tier
        if call.tool in registry:
            effective_tier = max(ctx.tier, registry.spec(call.tool).tier)

        with tracer.start_as_current_span(APPROVAL) as span:
            span.set_attribute("gen_ai.tool.name", call.tool)
            span.set_attribute("harness.tier", str(effective_tier))
            approval = await gate.review(
                ApprovalRequest(
                    run_id=ctx.run_id,
                    principal=ctx.principal,
                    tool=call.tool,
                    arguments=call.arguments,
                    tier=effective_tier,
                    rationale=call.rationale,
                    steps_taken=ctx.step_count,
                )
            )
            # `gated` and `approved` are separate facts: "nobody looked because
            # it was below the threshold" and "somebody looked and said yes"
            # are very different sentences in an audit.
            span.set_attribute("harness.gated", approval.gated)
            span.set_attribute("harness.approved", approval.approved)
            span.set_attribute("harness.approver", approval.approver)
            if not approval.approved:
                record_error(span, "not_approved", approval.reason or "refused")
        if not approval.approved:
            # Recorded before returning: an approval decision that leaves no
            # trace is indistinguishable from no approval at all.
            ctx.record(
                StepRecord(
                    index=ctx.step_count,
                    kind=StepKind.APPROVAL,
                    summary=f"{call.tool} refused by {approval.approver}",
                    tool_name=call.tool,
                    error=f"not approved: {approval.reason}",
                    metadata={"approver": approval.approver, "tier": int(effective_tier)},
                )
            )
            logger.warning(
                "run stopped by an approval gate",
                extra={
                    "run_id": ctx.run_id,
                    "tool": call.tool,
                    "approver": approval.approver,
                },
            )
            return _result(ctx, RunOutcome.NOT_APPROVED, error=f"{call.tool}: {approval.reason}")

        if approval.gated:
            ctx.record(
                StepRecord(
                    index=ctx.step_count,
                    kind=StepKind.APPROVAL,
                    summary=f"{call.tool} approved by {approval.approver}",
                    tool_name=call.tool,
                    metadata={"approver": approval.approver, "tier": int(effective_tier)},
                )
            )

    return None


async def _run_one(call: CallTool, registry: ToolRegistry, ctx: RunContext) -> StepRecord:
    """Perform one tool call and describe what happened.

    Returns a record rather than writing one. That is what lets a batch be
    recorded in the order it was planned instead of the order it finished;
    `index` is filled in by the caller at record time.

    Failures are described and fed back to the planner rather than ending the
    run -- that feedback *is* self-correction. What stops an agent retrying
    forever is the consecutive-failure ceiling, not this function.
    """
    spec_timeout: float | None = None
    tier = None
    if call.tool in registry:
        spec = registry.spec(call.tool)
        spec_timeout = spec.timeout_seconds
        tier = spec.tier
    budget = ctx.timeout_for(spec_timeout)
    started = time.perf_counter()

    # One span per call. In a batch these are siblings under the same plan
    # span, so an overlapping timeline is what parallelism looks like in a
    # backend -- and a serialised one is what a regression looks like.
    with tracer.start_as_current_span(TOOL) as span:
        span.set_attribute("gen_ai.tool.name", call.tool)
        span.set_attribute("harness.timeout_s", budget)
        if tier is not None:
            span.set_attribute("harness.tool.tier", str(tier))

        def _record(
            summary: str,
            *,
            error: str | None = None,
            result: Any = None,
            cost_usd: float = 0.0,
        ) -> StepRecord:
            return StepRecord(
                index=-1,  # assigned by the caller
                kind=StepKind.TOOL_CALL,
                summary=summary,
                tool_name=call.tool,
                arguments=call.arguments,
                result=result,
                error=error,
                cost_usd=cost_usd,
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        try:
            # Already authorised above. `call` rather than `invoke` so the
            # permission check is not repeated; the rate limit is consumed
            # here, which is why a replay -- which overrides `call` -- costs
            # nothing. See ADR-0017.
            value = await asyncio.wait_for(registry.call(call.tool, call.arguments), budget)
        except TimeoutError:
            # A timeout is a failed action, not a failed run. The downstream may
            # simply be slow and the planner may have another route.
            record_error(span, "timeout", f"timed out after {budget:.1f}s")
            return _record(f"{call.tool} timed out", error=f"timed out after {budget:.1f}s")
        except ToolDeniedError as exc:
            # A denial is the system working, so it goes back to the planner:
            # there may be a legitimate alternative route. It still counts
            # toward the failure ceiling, because an agent that probes denials
            # indefinitely is a security problem rather than a persistent one.
            record_error(span, "denied", exc.reason)
            return _record(f"{call.tool} denied", error=f"denied: {exc.reason}")
        except (ToolNotFoundError, RateLimitExceededError) as exc:
            record_error(span, "unavailable", f"{type(exc).__name__}: {exc}")
            return _record(f"{call.tool} unavailable", error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            # The tool itself failed. Also an observation, not a crash -- a
            # flaky downstream is exactly what self-correction is for.
            record_error(span, "failed", f"{type(exc).__name__}: {exc}")
            return _record(f"{call.tool} failed", error=f"{type(exc).__name__}: {exc}")

        return _record(f"{call.tool} ok", result=value, cost_usd=call.estimated_cost_usd)
