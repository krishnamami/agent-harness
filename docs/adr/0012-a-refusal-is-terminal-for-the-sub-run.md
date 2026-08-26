# ADR-0012: A refusal is terminal for the sub-run, not for the tree

- **Status:** Accepted
- **Date:** 2026-08-26

## Context

ADR-0008 settled what happens when a human refuses a call inside a run: the
run ends. Feeding the refusal back as an observation would produce an agent
that rephrases its request until someone says yes, which is worse than having
no gate at all.

Delegation raises the same question one level up, and the answer is not
automatically the same. A worker's sub-run is refused. What should the
coordinator see?

There are two coherent answers and they lead to very different systems.

## Decision

**A refusal is terminal for the sub-run and arrives at the parent as an
observation.** The parent's record gets one failed `DELEGATION` step, its
consecutive-failure count goes up by one, and the run continues.

The reasoning is that a reviewer refusing a delegation is saying no to *that
route*, not to the objective. A coordinator asked to resolve a customer's
balance query, whose request to pull a document from a restricted store is
refused, has not been told to abandon the query — it has been told it cannot
have that document. Trying a different evidence source is the correct
behaviour, and is what a competent human colleague would do.

The opposite reading — a refusal anywhere kills the whole tree — makes any
single cautious reviewer a denial of service on the root goal. In a system
where reviewers are asked to approve dozens of things a day, that is not a
theoretical failure mode; it is the one that turns the gate off.

## Consequences

- The `DELEGATION` step's error carries the sub-run's outcome, so the trace
  distinguishes "the coordinator gave up" from "the coordinator was refused
  and went another way".
- Because a refusal counts as one failure, a coordinator that keeps hitting
  refusals reaches the give-up ceiling from ADR-0005 on its own. That ceiling
  is the backstop for this decision, not an unrelated bound.

## The risk this creates, named

The pathology ADR-0008 exists to prevent — rephrase until someone says yes —
can reappear one level up. Nothing in the harness stops a coordinator
re-delegating an identical sub-goal to a second worker after the first is
refused, and that is functionally the same behaviour with an extra layer of
indirection.

Three things bound it, and none of them is complete:

- The consecutive-failure ceiling. A coordinator gets a small number of
  refusals before the run ends.
- Cost and time are drawn, so retrying is not free.
- Every refusal is in the trace with its approver, so the pattern is visible
  after the fact.

What is *not* implemented is a check that a parent is not re-delegating a
sub-goal materially identical to one already refused. It requires deciding what
"materially identical" means, which is a service-level judgement rather than a
harness one, and guessing at it here would produce a check that is either
trivially evaded or fires on legitimate retries. It is recorded as a known gap
rather than closed badly.

## Rejected alternatives

**A refusal anywhere ends the whole tree.** Simpler, safer-sounding, and
defensible — it is the conservative reading, and an organisation that wants it
can get it by having its gate refuse at the root. Rejected as the *default*
because it conflates "no to this route" with "no to this objective", and
because a gate that can kill an entire multi-step run from any leaf is a gate
people route around. The point of tiered oversight in ADR-0008 was that a gate
which is too blunt gets switched off.

**Feed the refusal back to the child's planner as an ordinary observation.**
This is exactly what ADR-0008 rejected, and it is no better inside a sub-run.

**A distinct outcome for the parent, say `CHILD_REFUSED`.** More precise, and
it would let a service branch on it. Rejected because the parent's step already
carries the child's outcome in its metadata, and adding parent-side outcomes
for each way a child can end multiplies the enum by the number of failure modes
without telling anyone anything the trace does not already say.
