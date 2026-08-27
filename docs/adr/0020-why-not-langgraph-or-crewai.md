# ADR-0020: Why this exists when LangGraph and CrewAI do

## Status

Accepted.

## Context

The question arrives in every review of this repository, usually phrased as an
accusation: *you rebuilt an agent framework.* It deserves a real answer rather
than a list of things the alternatives cannot do, because most of that list
would be wrong.

What they actually provide, stated fairly.

**LangGraph** is a graph runtime. Nodes, edges, a typed state object, and a
checkpointer that persists state per thread. Its `interrupt()` mechanism pauses
execution anywhere in a node and waits for a `Command(resume=...)` against the
same thread id — the documentation names approval of financial transactions and
review or editing of a tool call before it executes as intended uses. That is a
genuine human-in-the-loop facility and it is better than nothing by a wide
margin. Its persistence is also real: a paused graph survives a process restart.

**CrewAI** is a level higher — agents with roles, tasks, and a crew that
coordinates them. It optimises for getting a multi-agent workflow running
quickly, and it does that well.

Neither is a toy, and this repository does not claim to have outgrown them.

## The actual disagreement

LangGraph's own documentation is explicit that risk tiering, authorisation
policies and audit trails are not provided and belong in your node code. That is
an honest scoping statement, not a defect. But it locates the thing this
repository is about squarely outside the framework, and the shape of what is
left over matters more than the fact that it is left over.

**`interrupt()` is placed by the graph author, inside a node.** Oversight is
therefore a property of *where someone remembered to put a call*. A new node that
performs a refund and omits the interrupt is not an error — it is a graph that
runs. Nothing compares the action against a policy and notices the omission,
because there is no declaration of what this action's risk is for anything to
compare against.

In this harness, oversight is a property of the tool's declared `tier`, enforced
on the only path that reaches the tool. `ToolRegistry.invoke` is the single door;
there is deliberately no way to reach the underlying function without passing
through it. You cannot forget to gate a consequential call, because gating is not
something the caller does.

That is the same distinction ADR-0003 in `tool-registry` draws between the
harness and the server: **a convention that must be followed versus a boundary
that cannot be skipped.** A framework whose safety property is "put the
`interrupt()` in the right places" has a convention. It is a good convention, and
it is the one that fails on the Tuesday when someone adds a node in a hurry.

Three further things follow from the same root and are not incidental:

- **Delegation that narrows.** A sub-agent receives strictly less authority than
  its parent, enforced rather than documented (ADR-0011). A framework with no
  authorisation model has nothing to narrow.
- **Replay through the real executor.** A recorded run is re-executed through the
  same code path, and a divergence is reported (ADR-0007). This is an audit
  facility, not a resumption facility — see the honest gap below.
- **Redaction as policy.** Whether a step's arguments are recorded is an audit
  policy decision keyed to the tool, and a batched call was very nearly a route
  around it (ADR-0013). That mechanism has nowhere to live in a graph runtime.

## Decision

Build the execution boundary; do not adopt a framework as the control layer;
stay composable with one.

The harness ships **no orchestration DAG** on purpose. Deciding which sub-goals
exist, in what order, with what retries, is scheduling, and a harness that also
schedules is two products. So the honest position is not *harness instead of
LangGraph* — it is **harness inside LangGraph**, when the workflow is genuinely a
graph. A LangGraph node that runs `run_agent` against a bounded registry is a
coherent architecture and the one to reach for when the problem has a shape worth
drawing.

CrewAI is the poorer fit for this domain specifically, and for a reason that is
to its credit elsewhere: it abstracts the loop. The loop is the thing that needs
bounding here — step ceilings, cost ceilings, wall-clock ceilings, a
consecutive-failure ceiling, and a give-up condition — and an abstraction over it
is an abstraction over the controls.

## Consequences

**What was given up, plainly.**

- **No ecosystem.** Every integration LangChain ships for free is ours to write.
- **We own the loop and its bugs.** Six of the eleven defects listed in the
  README are in code a framework would have supplied. That is a real cost, and
  the counter-argument is only that we found them.
- **Durable resume is genuinely missing.** LangGraph's checkpointer restores a
  paused graph after a crash. Our replay re-executes a *finished* run for audit;
  it does not resume an interrupted one. A run killed mid-flight is gone here and
  recoverable there. That is the strongest single argument for LangGraph and it
  should not be waved away.

It is worth noting that checkpointing is not the same as durable execution in
either framework — LangGraph has no coordination preventing two processes
resuming the same `thread_id` concurrently, and CrewAI's `@persist` does not skip
already-completed steps without hand-written conditionals. So "adopt LangGraph
and get reliability" is not a complete answer either; it is a better starting
point for one.

**What was gained** is a system where the safety properties are structural rather
than conventional, and where each one is written down with the defect that
produced it. For an agent that can move money, that trade is the right way round.
It would be the wrong way round for a prototype, and this ADR should be re-read
by anyone tempted to start here for something that is not consequential.

## Rejected alternatives

**Adopt LangGraph and layer policy on top.** The layer only holds if it owns the
only path to the tool. Owning that path means owning the registry, the
authorisation check, the gate and the audit record — which is this repository,
now sitting inside a graph runtime we also depend on. Considered seriously; the
right version of it is the composition above, not a wrapper.

**Adopt CrewAI and constrain it.** The constraints have to reach inside the loop,
and the loop is what the abstraction hides.

**Wait for a framework to grow these features.** Possible, and if one does, the
ADRs here become the evaluation criteria rather than a justification for having
built it.
