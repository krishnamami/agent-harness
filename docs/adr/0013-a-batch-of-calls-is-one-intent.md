# ADR-0013: A batch of calls is one intent

- **Status:** Accepted
- **Date:** 2026-08-26

## Context

`Planner.decide()` returned exactly one `Decision`. Every current model
tool-use API returns *several* tool calls per turn, so a planner that wants
three independent lookups had to serialise them into three loop turns — paying
three planning round-trips for work with no ordering between it at all. On the
p95 numbers this harness is supposed to be able to publish, that is most of the
latency, and none of it is doing anything.

The concurrency itself is easy. What is not easy is keeping the properties the
rest of this harness depends on once calls stop happening one at a time: a
bounded run, a deterministic trace, an audit policy that actually holds, and a
gate whose refusal means something.

## Decision

A new decision variant, `CallTools`, carrying a tuple of `CallTool`. Not a list
return type from `decide()` — that would break every existing planner and make
"one decision" ambiguous in the protocol.

**A batch is one intent.** One `PLAN` step, carrying the calls in metadata.
Recording a `PLAN` per call would claim the planner took several turns, and a
replay reconstructed from that record would take several turns too.

**Afforded as a whole.** The projected cost is the *sum*, checked before
anything runs. Cost cannot be un-spent, so a batch that would breach the
ceiling is refused entire rather than executed up to the line and abandoned.
Step room is checked for the whole batch for the same reason: running half of
something planned as a unit leaves the planner reasoning about a state that
never existed.

**Authorised per call.** Unlike cost, a denial spends nothing, so the permitted
calls still run and the planner learns both facts in one turn instead of
burning another on the half that was always going to work.

**Gated as a whole.** A refusal anywhere stops the batch. ADR-0008 makes a
refusal terminal for the run, and "terminal except for the other three calls
asked for in the same breath" is not terminal.

**Recorded in the order declared.** `asyncio.gather`, which preserves argument
order, rather than `as_completed`. Completion order is a property of the
network on the day; a trace that reflected it would not replay to the same
thing twice, and deterministic replay is the claim in ADR-0007.

**The fan-out is bounded.** `max_parallel_calls` (default 8). A planner asking
for five hundred concurrent calls is a runaway of exactly the kind ADR-0003
exists to prevent. An over-wide batch is recorded as a `CORRECTION` — a plan
the run will not carry out, rather than a call that failed — so the planner can
split it, and it counts as a failure so one that keeps asking runs out of
patience rather than out of turns.

## Consequences

- `_invoke` became `_run_one`, which *returns* a `StepRecord` instead of
  writing one. That inversion is what makes order-of-declaration recording
  possible, and it made the serial path simpler too.
- `StepKind.CORRECTION` had been defined and never used since session 1. It
  now has exactly one meaning: the harness declining a plan rather than a call.
- A single call still records exactly the step it used to. Existing traces and
  existing planners are unaffected.

## Two bugs this found

**Step metadata was never serialised.** `RunTrace.to_dict` wrote index, kind,
summary, tool_name, arguments, result, error and cost — and dropped
`metadata`. Nothing had depended on it, so nothing had failed. But
`arguments_withheld` lives in metadata, which means the ADR-0007 guard —
*this trace can be inspected but not replayed* — silently disappeared the
moment a trace was written to disk and read back. A reloaded trace replayed
withheld arguments as an empty dict instead of refusing. A guard that
evaporates on serialisation is not a guard, and this one had been broken since
the trace format was written.

**The audit policy did not reach inside a batch.** Redaction keyed off
`step.tool_name`, which is `None` on a batched `PLAN` step because the tools
live in metadata. Left alone, batching would have been a route around the audit
policy: same tool, same arguments, recorded in full because they happened to
arrive in a list. Redaction is now per call within the batch, and a batch with
any call withheld is marked unreplayable.

Both were found by asking what a *batch* would do to an existing mechanism,
rather than by testing the batch on its own.

## Rejected alternatives

**`decide()` returns a list of decisions.** The obvious shape, and it breaks
every planner already written against the protocol. It also makes the return
type say something false: a `Finish` in a list alongside two calls has no
coherent meaning.

**One `PLAN` step per call.** Simpler recording, and it lies about how many
turns the planner took. Replay would faithfully reproduce turns that never
happened.

**Check cost per call as each one starts.** Cheaper to implement and it runs
two calls before discovering the third is unaffordable. There is no way to
un-spend the first two.

**Refuse the whole batch when any call is unauthorised.** Symmetrical with the
cost rule, and pointlessly wasteful: the denial cost nothing, and discarding
the permitted work makes the planner take another turn to learn what it could
have known now.

**Let siblings proceed when one call is refused by the gate.** Would make a
refusal mean "no, unless you asked for other things at the same time", which
is an obvious way to launder a refused call into a batch.

**`as_completed`, to feed results back as they arrive.** Lower latency to the
first result, and it makes the trace order non-deterministic. Replay would
diverge on timing alone, which would make every genuine divergence unreadable
against the noise.

**No fan-out ceiling.** Every other resource in this harness is bounded by
construction; leaving concurrency unbounded because it "comes from the planner"
is exactly the reasoning ADR-0003 rejects.
