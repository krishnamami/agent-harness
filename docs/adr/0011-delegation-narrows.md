# ADR-0011: Delegation narrows; it never widens

- **Status:** Accepted
- **Date:** 2026-08-26

## Context

Both use cases this harness is being built to carry are multi-agent. A
coordinator reads a work list and hands sub-goals to workers; an adjudicator
decides on what they bring back. Neither is expressible with `run_agent` alone.

The obvious implementation is a function that starts a second run and returns
its result. It works on the first day. By the second it is a
privilege-escalation path, because nothing in it stops a child asking for more
than its parent holds — more roles, more budget, more time, or another
delegation ten levels down. "The sub-agent runs as an admin service account"
is how an agent platform becomes an exfiltration route, and it is usually
nobody's decision: it is what happens when delegation is a convenience function.

The signature is the easy part. The invariants are the design.

## Decision

Delegation is `src/harness/delegation.py`, with five invariants.

**A principal may only narrow.** A child's roles must be a subset of its
parent's. It may *add* a purpose where the parent declared none — under
permissible-purpose authorisation a principal with no declared purpose is
denied outright, so declaring one narrows — but it may not change a purpose
already declared, and it may not introduce or contradict an attribute.

**Cost and time are drawn, not granted.** They are fungible: a dollar the child
spends is a dollar the parent no longer has. A child's ceilings are capped at
the parent's remaining, so total spend across a tree of any shape stays bounded
by the root. This is the property a finance function actually asks about, and
it is the only version of the answer that survives a tree.

**Steps are not drawn.** A step is one turn of one loop. The child's twenty
turns are not the parent's, and capping the child at the parent's *remaining*
steps would mean a coordinator near the end of its own plan could not delegate
at all — which is exactly when a coordinator delegates.

**Depth bounds the tree.** `max_delegation_depth` is taken from the parent
rather than from the requested limits, so a branch cannot buy itself more room
by asking for it on the way down.

**A delegation is one step in the parent's record.** Not the child's twenty. It
carries the child's whole cost, which is what charges the parent, and it is
marked failed if the sub-run did not complete.

Two entry points. `new_child_context` is the invariant-enforcing constructor;
`delegate` is the common case that also runs and records.

Budget exhaustion returns a named outcome, the way every other ceiling in this
harness does. Privilege escalation *raises*, because it is not a runtime
condition — it is a defect in the calling service, and reporting it quietly as
a failed run would let it ship.

## Consequences

- `RunLimits` gained `max_delegation_depth`; `RunOutcome` gained `DEPTH_LIMIT`;
  `StepKind` gained `DELEGATION`; `RunContext` gained `depth` and `sub_runs`.
- The trace serialises the new limit with a default on read, so a trace
  recorded before this ADR still loads.
- Nesting sub-traces into one tree trace, and replaying a tree, is **not** in
  this change. The linkage exists in both directions — the parent's step
  carries `child_run_id`, the child's metadata carries `parent_run_id` and
  `parent_step_index` — but assembling and replaying the tree is its own piece
  of work.

## Rejected alternatives

**Draw steps as well as cost and time.** Symmetrical, and wrong. It conflates a
structural quantity with a fungible one, and it disables delegation precisely
when a coordinator needs it.

**Give the child an independent budget.** Simplest to implement and it destroys
the only property that makes a tree safe: the root would stop bounding
anything, and total spend would be the product of the branching factor and the
depth rather than a number anyone chose.

**Append the child's steps to the parent's step list.** Tempting, because it
gives one flat record. It breaks the consecutive-failure ceiling from ADR-0005:
one failed sub-run of twenty steps would register as twenty consecutive
failures and trip the parent's give-up ceiling on its own. It also stops the
parent's record being a record of the parent's actions.

**Delegation as a registered tool.** Genuinely attractive: it would inherit
authorisation, risk tiering, the approval gate and the failure streak for free,
with no new concepts. Rejected on two counts. The executor would record a
`TOOL_CALL` and the delegation would record a `DELEGATION`, double-counting the
step and the streak; and the cost charged to the parent would be the planner's
*estimate* rather than the child's actual spend, which quietly breaks the
drawn-budget invariant that is the whole point. Worth revisiting if the
executor ever learns to reconcile a step's estimated cost with its actual one.

**`delegate` alone, with no `new_child_context`.** This was the original
design, and writing the tests found the hole: nothing hands a child its own
`RunContext`, so nothing below depth one could delegate further. The depth
ceiling would have been enforced by accident — capped at 1 regardless of
configuration — and the bug would have surfaced as "why does
`max_delegation_depth=3` behave like 1" in whichever service hit it first.
