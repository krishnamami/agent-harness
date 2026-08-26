"""The executor side of the split.

Runs the loop, enforces every bound, and records what happened. The planner
proposes; this decides whether the proposal is allowed to happen.

Nothing here calls a model. Nothing in the planner calls a tool. That is the
whole architecture.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from harness.planner import CallTool, Finish, Planner, PlannerState
from harness.run import (
    CostLimitExceededError,
    RunContext,
    RunOutcome,
    RunResult,
    StepKind,
    StepLimitExceededError,
    StepRecord,
)
from harness.tools import (
    RateLimitExceededError,
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
) -> RunResult:
    """Run until the agent finishes or a bound stops it.

    Every exit is a named outcome. A run that hits a ceiling is not a failed
    run — `STEP_LIMIT`, `COST_LIMIT` and `GAVE_UP` are the system working, and
    counting them separately from `FAILED` is what lets you tune the ceilings
    instead of guessing at them.
    """
    logger.info(
        "run started",
        extra={"run_id": ctx.run_id, "goal": goal, "tier": int(ctx.tier), "planner": planner.name},
    )

    while True:
        # --- can we afford another step at all? -------------------------
        try:
            ctx.before_step()
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
        try:
            decision = await planner.decide(state)
        except Exception as exc:
            # A planner that raises ends the run. There is no sensible way to
            # continue without knowing what to do next, and retrying a planner
            # that just crashed is how you spend a budget on stack traces.
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

        # --- a tool call ------------------------------------------------
        ctx.record(
            StepRecord(
                index=ctx.step_count,
                kind=StepKind.PLAN,
                summary=decision.rationale or f"call {decision.tool}",
                cost_usd=decision.cost_usd,
                duration_ms=plan_ms,
            )
        )

        # Projected cost is checked before the call, not after. A run that
        # discovers it is over budget has already spent it.
        try:
            ctx.before_step(projected_cost_usd=decision.estimated_cost_usd)
        except StepLimitExceededError as exc:
            return _result(ctx, RunOutcome.STEP_LIMIT, error=str(exc))
        except CostLimitExceededError as exc:
            logger.warning(
                "refused a call that would breach the cost ceiling",
                extra={"run_id": ctx.run_id, "tool": decision.tool},
            )
            return _result(ctx, RunOutcome.COST_LIMIT, error=str(exc))

        outcome = await _invoke(decision, registry, ctx)
        if outcome is not None:
            return outcome

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


async def _invoke(decision: CallTool, registry: ToolRegistry, ctx: RunContext) -> RunResult | None:
    """Perform one tool call. Returns a RunResult only if the run must end.

    Failures are recorded and fed back to the planner rather than ending the
    run — that feedback *is* self-correction. What stops an agent retrying
    forever is the consecutive-failure ceiling, not this function.
    """
    started = time.perf_counter()
    try:
        result = await registry.invoke(decision.tool, decision.arguments, ctx.principal)
    except ToolDeniedError as exc:
        # A denial is the system working, so it is recorded and returned to
        # the planner: there may be a legitimate alternative route. But it
        # counts toward the failure ceiling, because an agent that probes
        # denials indefinitely is a security problem rather than a persistent
        # one.
        ctx.record(
            StepRecord(
                index=ctx.step_count,
                kind=StepKind.TOOL_CALL,
                summary=f"{decision.tool} denied",
                tool_name=decision.tool,
                arguments=decision.arguments,
                error=f"denied: {exc.reason}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return None
    except (ToolNotFoundError, RateLimitExceededError) as exc:
        ctx.record(
            StepRecord(
                index=ctx.step_count,
                kind=StepKind.TOOL_CALL,
                summary=f"{decision.tool} unavailable",
                tool_name=decision.tool,
                arguments=decision.arguments,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return None
    except Exception as exc:
        # The tool itself failed. Also an observation, not a crash — a flaky
        # downstream is exactly the situation self-correction is for.
        ctx.record(
            StepRecord(
                index=ctx.step_count,
                kind=StepKind.TOOL_CALL,
                summary=f"{decision.tool} failed",
                tool_name=decision.tool,
                arguments=decision.arguments,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return None

    ctx.record(
        StepRecord(
            index=ctx.step_count,
            kind=StepKind.TOOL_CALL,
            summary=f"{decision.tool} ok",
            tool_name=decision.tool,
            arguments=decision.arguments,
            result=result,
            cost_usd=decision.estimated_cost_usd,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    )
    return None
