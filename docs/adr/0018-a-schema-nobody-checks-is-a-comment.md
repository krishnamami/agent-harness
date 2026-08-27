# ADR-0018: A schema nobody checks is a comment

## Status

Accepted. Fixes behaviour present since the first commit.

## Context

`ToolSpec` has always carried a `parameters` JSON Schema, and the module
docstring of `tools.py` has always said why:

> Every tool declares a JSON Schema for its arguments. Not for documentation —
> so the harness can reject a malformed call before it reaches a real system.

It did not. `check()` looked up the tool, asked the authorisation policy, and
returned the spec. Nothing ever read `parameters`. A tool declaring
`amount: integer`, `payment_id` required, `additionalProperties: false` accepted
all of this:

```
ACCEPTED  {'amount': 5}                                    # no payment_id
ACCEPTED  {'amount': 'five', 'payment_id': 'P1'}           # a string amount
ACCEPTED  {'amount': 5, 'payment_id': 'P1',
           'drop_tables': True}                            # undeclared
ACCEPTED  {}                                               # nothing at all
invoke -> refunded five
```

The last line is a refund issued for `"five"`.

**Why it stayed invisible.** Every test and every README example drives
`ScriptedPlanner`, where a person writes the arguments and naturally writes
them correctly. The schema is only load-bearing when a model produces the
arguments, which is the one thing the harness exists to make safe. The
repository had 286 passing tests and not one of them asserted that a malformed
call is refused; adding validation broke none of them, which is the clearest
possible statement of the gap.

This is the same class of defect as ADR-0017: a guarantee the code claims in
prose and does not implement, invisible under the conditions the tests happen
to create.

## Decision

**Validate arguments against the declared schema in `check()`.** A violation
raises `ToolArgumentError`, which carries the specific problems rather than a
bare "invalid arguments" — a planner handed the latter resubmits the same call.

**Validation runs before authorisation.** This is the part worth arguing.

An authorisation policy receives the arguments and is entitled to read them;
`AuthorizationPolicy.authorize(principal, tool_name, arguments)` exists in that
shape precisely so a deployment can write "refunds over five hundred need a
second approver". Such a policy, handed `{"amount": "five"}` or arguments with
no `amount` at all, evaluates `arguments.get("amount", 0) > 500` to `False` and
**permits** the call. Not a crash — a silent permission, which is worse. Every
policy has the right to assume the arguments conform to the contract it was
written against, and the only way to give it that right is to check first.

The cost is that a caller who may not use a tool at all can still learn that
its arguments were malformed. That is a narrower disclosure than it sounds:
`describe_for(principal)` already governs which schemas a principal can see,
the caller had to name the tool to get here, and the message names the
violation rather than the schema. Correct policy evaluation is worth more.

**Every violation is reported, sorted, and capped.** All of them, because a
planner told about one missing field at a time spends a model call per field.
Sorted, because error order is part of a replay being deterministic. Capped at
five, because the text goes into a prompt.

**The schema itself is checked at `ToolSpec` construction.** A tool with a
malformed schema is broken where it is written, not on the first request that
happens to exercise it.

**`ToolArgumentError` is an observation, not a crash.** The executor catches it
in the same pre-flight pass that catches denials, records a failed `TOOL_CALL`
step, and lets the planner see the violations through `PlannerState.last_error`.
Because it is recorded as a failed action rather than a plan, it moves the
consecutive-failure counter — so a model that keeps emitting the same malformed
call reaches the give-up ceiling instead of burning the whole step budget.

## Consequences

A malformed call is filtered before the approval gate, for the same reason a
denied one is: an approver asked to sign off a call that cannot succeed is an
approver being trained that approvals are inconsequential.

`jsonschema` becomes a runtime dependency. A hand-rolled subset would have been
the wrong kind of clever — the same schemas are what model providers consume,
so "what the harness enforces" and "what the model was told it may send" have
to be the same document under the same reading of the specification. Divergence
there is a bug that presents as the model being wrong.

The validator is compiled once at registration rather than per call. Invisible
in a test, measurable in a loop making thousands.

`call()` still does not validate. It is documented as reachable only by a
caller that has just checked, and it does not re-run authorisation either;
adding one and not the other would be incoherent. `ReplayRegistry` overrides
`call`, and a replay validates through `check` like any other run.

## Rejected alternatives

**Validate in `call()` instead.** It would catch direct callers, and it would
be too late for everything that matters: the pre-flight pass, the approval
filter, and the planner's correction signal all happen before `call`.

**Validate after authorisation.** Cheaper to defend on disclosure grounds and
wrong on the point that matters — it leaves every argument-reading policy
judging data it cannot trust.

**Report only the first violation.** Smaller messages, more model calls, and a
correction loop that converges one field at a time.

**Leave it and document the limitation.** The docstring already documented the
behaviour. The behaviour was absent. That is not a limitation.
