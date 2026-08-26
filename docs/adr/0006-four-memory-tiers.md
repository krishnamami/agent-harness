# ADR-0006: Four memory tiers, not one store

- **Status:** Accepted
- **Date:** 2026-08-26

## Context
Most implementations put everything in one vector store and call it "memory".
That works until someone asks a question the store cannot answer.

## Decision
Four tiers with genuinely different lifecycles:

| Tier | Lives | Typically about | Retention |
|---|---|---|---|
| **Working** | one run | the task in hand | none — dies with the run |
| **Episodic** | across runs | *what happened*, keyed by subject | bounded, erasable |
| **Semantic** | indefinitely | domain facts | usually indefinite |
| **Procedural** | indefinitely | tool sequences that worked | indefinite |

**Episodic is why this matters.** "What is our memory retention policy" has one
answer for facts about mortgage products and a different answer for a record of
what an agent did while looking at a named consumer's file. A single store must
either apply the strictest obligation to everything — expensive and lossy — or,
far more commonly, apply none of it.

Consequences of the split that are load-bearing:

- **`subject` is a first-class field and `forget(subject)` is in the protocol**,
  not an administrative extra. A store that can write but not selectively erase
  is unusable anywhere a subject can ask to be removed, and finding that out
  later means rebuilding it. `forget_subject` returns per-tier counts, because
  "we deleted it" is not an answer anyone accepts and "14 episodic, 2 semantic"
  is.
- **Retention is enforced on the read path**, not only by pruning. Expired
  records are filtered out of `recall`, so the guarantee holds even if the
  prune job never runs. Pruning reclaims space; the filter is the promise.
- **Working memory is bounded and is deliberately not a `MemoryStore`.**
  Unbounded working memory is how a long run walks into a context failure at
  step forty having already spent the budget. It is excluded from the store
  protocol so nobody swaps in a durable backend and quietly creates a record
  nobody is managing. Eviction is counted and exposed — silent eviction looks
  exactly like a model ignoring its context, and the two are debugged very
  differently.
- **Procedural memory records only successful trajectories.** A store of
  successes and failures is one an agent cannot learn from without first
  learning which is which.
- **Writing to the wrong tier raises.** A record in the wrong store inherits
  the wrong retention policy, which is precisely how episodic data ends up
  living forever.

## The bug this ADR exists because of
`Memory.__init__` used `episodic or InMemoryStore(...)`. These classes define
`__len__`, so an **empty store is falsy** — a store handed in by a caller, with
its retention policy, was silently discarded for the default. The bug is
invisible until the store has records in it, by which point they have been
written under the wrong retention. Three regression tests pin it, including one
asserting that a *non-empty* store was never affected, which is exactly why it
survived the first pass.

`x or default` is unsafe for any object defining `__len__` or `__bool__`. Use
`x if x is not None else default`.

## Alternatives considered
- **One vector store for everything.** Simpler, and cannot answer a retention
  or erasure question per data type.
- **Two tiers, short-term and long-term.** Better, and still merges "what
  happened to this person" with "what is true about the domain".
- **Storing failed trajectories too, with a flag.** Every reader then has to
  remember to filter, and one that forgets teaches the agent to repeat failures.
- **Making `WorkingMemory` a `MemoryStore`.** Uniform, and invites exactly the
  substitution that creates an unmanaged record.

## Consequences
A service supplies a backend per tier and they need not be the same product —
episodic in Postgres because it must be erasable and auditable, semantic in a
vector store because it is searched by meaning. The in-memory reference
implementation is correct but not durable; its purpose is to let a service
exercise the tier boundaries, the retention policy and the erasure path before
choosing a database.
