# ADR-0005: A failure is an observation; a streak is a stop

- **Status:** Accepted
- **Date:** 2026-08-26

## Context
When a tool call fails, the run can abandon or retry. Abandoning is brittle —
a flaky downstream ends a run that would have succeeded on a second attempt,
and there is frequently a legitimate alternative route. Retrying without a
bound is worse: "self-correcting" then means "fails slowly and expensively".

## Decision
A failed tool call is **recorded and fed back to the planner** through
`PlannerState.last_error`. That feedback is the whole of self-correction from
the planner's side. What stops the retrying is the **consecutive-failure
ceiling**, checked by the executor.

The same treatment applies to an authorisation denial, deliberately. A denial
is the system working, so the planner sees it and may choose another route —
but it counts toward the ceiling, because an agent that probes denials
indefinitely is a security problem rather than a persistent one.

Two failures are **not** observations and end the run:

- **A planner that raises.** There is no sensible way to continue without
  knowing what to do next, and retrying a planner that just crashed spends the
  budget on stack traces.
- **A breach of a ceiling.** By definition.

## The bug this ADR exists because of
The first implementation reset the failure streak on any successful step. The
loop records a `PLAN` step before every `TOOL_CALL`, so the counter oscillated
0-1-0-1 between every failed call and the ceiling was never reached. A runaway
agent burned its entire 25-step budget instead of stopping after three
failures — the give-up condition was present, tested, and silently inert.

The streak now counts consecutive failed **actions**; planning is neutral.
`test_a_planning_step_does_not_reset_the_failure_streak` pins it.

The general lesson is worth keeping: a counter that is reset by an event you
did not think of is indistinguishable from a counter that works, until the
thing it was guarding actually happens.

## Alternatives considered
- **Abandon on first failure.** Brittle against transient downstream errors,
  and gives up on routes that would have worked.
- **Retry the same call with backoff.** Right for a network blip, wrong for an
  agent — the planner may need to do something *different*, and only it can
  decide that.
- **Treat a denial as fatal.** Defensible, and it makes an agent unable to
  recover from a reasonable first guess about which tool to use.
- **Count total failures rather than consecutive ones.** Kills long runs that
  are making progress with occasional hiccups.

## Consequences
`RunOutcome` distinguishes `COMPLETED`, `STEP_LIMIT`, `COST_LIMIT`, `GAVE_UP`
and `FAILED`. Hitting a ceiling is the system working and is counted
separately, which is what makes the ceilings tunable from evidence rather than
from guesswork.
