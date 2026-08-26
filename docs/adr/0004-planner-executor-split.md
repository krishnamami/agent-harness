# ADR-0004: The planner proposes, the executor disposes

- **Status:** Accepted
- **Date:** 2026-08-26

## Context
The obvious way to build an agent loop is one function that asks a model what
to do and then does it. It works, and it fuses two things that need to be
separable.

## Decision
A `Planner` returns an **intention** — `CallTool(name, arguments)` or
`Finish(output)`. It never performs one. The executor decides whether the
intention is allowed to happen, performs it, and records what occurred.

The planner has **no reference to the registry**. That is the point rather than
a detail: there is no route from "the model asked for it" to "the tool ran"
that skips authorisation. A planner able to invoke tools directly would be a
policy bypass one refactor away, and the bypass would be invisible in review.

Three further consequences follow:

- **`PlannerState` is a frozen value object, not the live `RunContext`.** A
  planner handed the context could mutate the budget constraining it, at which
  point the constraint is advisory.
- **The planner is offered only tools its principal may call.** Showing it
  everything and rejecting later burns a step and a model call to learn
  something the registry already knew.
- **The state includes `remaining_steps` and `remaining_usd`.** A planner that
  knows it has one step left can finish with a partial answer instead of
  starting something it cannot complete.

## Alternatives considered
- **One fused loop.** Simplest, and neither half is testable alone. When a run
  goes wrong you cannot say whether the plan was wrong or the execution was.
- **Give the planner the registry and let it call tools.** Every agent
  framework does this. It means authorisation depends on the planner choosing
  to route through it.
- **Pass the live context to the planner.** Convenient for building prompts,
  and it hands the thing being constrained a writeable reference to its own
  constraint.

## Consequences
An executor is testable with a `ScriptedPlanner` and no model at all — most of
this repository's loop tests run without a network call. A planner is a pure
function from state to intention and is testable the same way. `ScriptedPlanner`
ships with the harness rather than living in the tests, because every service
needs it for the same reasons.
