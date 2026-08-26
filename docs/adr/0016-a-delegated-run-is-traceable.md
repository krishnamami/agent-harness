# ADR-0016: A delegated run is traceable, and the tree is one record

- **Status:** Accepted
- **Date:** 2026-08-26

## Context

ADR-0007 says the question a regulated employer actually asks is *an agent did
something in March; explain it*, and that a trace is the decision path captured
completely enough to re-run.

ADR-0011 then added delegation, and `delegate()` kept only the child's
`RunResult` on the parent. A `RunResult` carries steps, outcome and cost. It
does not carry the limits that were in force, the principal the run acted as, or
the tier it ran at — those live on the `RunContext`, which `delegate()` created
internally and dropped on the way out.

So a delegated run could not be turned into a trace at all. Not a degraded
trace: none. The service never saw the child's context, so it had nothing to
call `record_trace` with, and the parent's record held a `DELEGATION` step
pointing at a `child_run_id` that no longer resolved to anything.

Two ADRs, each defensible on its own, contradicting each other from the inside.
This was found while preparing to tag `v1.0.0` — which is the right time to find
it and a bad time to ship it.

## Decision

**`RunContext.sub_runs` holds `SubRun(context, result)`, not `RunResult`.** The
context is what makes a result explainable, so the parent keeps both.

**`record_trace` recurses.** A `RunTrace` gains `sub_traces`, recorded depth
first with the same registry and audit policy. The tree is nested rather than
flat: a list of sibling traces each carrying a parent id is a tree you have to
reassemble correctly before you can read it, and reassembly is where auditors
lose confidence.

**`RunTrace.walk()`** yields the trace and everything beneath it, depth first.

**The goal travels on the child's context**, so a sub-trace can say what it was
for without the parent having to be consulted.

**Replay stays per-run, deliberately.** The harness never *chose* to delegate —
the service did, through `delegate()` or `new_child_context()`, and a planner
has no way to express delegation as a `Decision`. Reproducing the tree's shape
is therefore the service's job, and claiming otherwise would be claiming to
replay code the harness has never seen. What the harness guarantees is narrower
and true: every run in the tree is recorded, and every run in the tree is
individually replayable. `walk()` is how you reach them.

## Consequences

- The trace format gained a key. Deserialisation reads it with `.get`, so a
  trace recorded before delegation existed still loads — the same rule as
  ADR-0010 and ADR-0013.
- Recording a tree costs one `record_trace` per run in it, ~18µs each at 25
  steps (ADR-0015). A four-worker coordinator pays under a tenth of a
  millisecond to be explainable.
- A service that gives a child a *different* registry should record that
  child's trace itself. The harness records with the registry it was handed and
  does not invent provenance it cannot vouch for.

## Rejected alternatives

**Leave it, and document that delegated runs are not traceable.** Honest, and
it guts the central claim. "Every run is explicable, except the ones inside a
tree" is not a property anyone will accept from a governance layer, and the
tree is where the interesting decisions happen.

**Have `delegate()` record the child's trace immediately and store that.**
Nearly chosen. It guarantees a trace exists, and it takes the decision away from
the service: a caller who does not want traces pays for them anyway, and a
caller who wants a different audit policy for children cannot have one. Keeping
the context and letting `record_trace` decide preserves both.

**Return the child context from `delegate()` alongside the result.** Makes the
common call site awkward for a case most callers do not need, and it leaves the
parent's own record incomplete — the tree still would not appear in the parent's
trace, only in whatever the caller remembered to keep.

**Flat traces with a `parent_run_id`, assembled on read.** How most systems do
it, and it works right up until someone assembles it wrong. A nested record has
one correct reading.

**Make delegation a planner `Decision` so replay could reproduce the tree.**
Tempting, because it would make the tree replayable end to end. Rejected on
ADR-0004 grounds: the planner proposes and the executor disposes, and a planner
that could spawn runs would be spawning runs under a principal the executor
never checked. The replay limitation is the price, and it is a smaller price
than a hole in the authorisation path.
