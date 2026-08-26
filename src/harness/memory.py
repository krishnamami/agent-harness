"""Memory, in four tiers.

Most implementations collapse this into one vector store and call it "memory".
The tiers have genuinely different lifecycles, different retention obligations
and different failure modes, and merging them means the strictest obligation
has to apply to all of it — or, more often, that none of them does.

    working     what is in this run's context right now.
                Bounded, ephemeral, dies with the run.

    episodic    what happened on previous runs.
                Keyed by subject. If the subject is a person, this is personal
                data, with everything that follows from that.

    semantic    facts about the domain.
                Long-lived, shared across runs, rarely about an individual.

    procedural  what has worked before — tool sequences that succeeded.
                The least-implemented tier, and the one that makes an agent
                improve rather than merely repeat.

The distinction that matters most in a regulated setting is episodic. A
question about "our memory retention policy" has one answer for facts about
mortgage products and a different answer for a record of what an agent did
while looking at a named consumer's file. One store cannot give both answers.

The harness ships the contracts and an in-memory reference implementation.
A production store — Redis, Postgres, a vector database — is the service's.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from harness.run import RunResult, StepKind

_DAY_SECONDS = 86_400.0


class MemoryTier(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


@dataclass(frozen=True)
class MemoryRecord:
    """One remembered thing.

    `subject` is the erasure key, and it is why this is not just a blob of
    text with an embedding. When someone asks for a record to be deleted, the
    question is "everything about this subject", and a store that cannot
    answer it cannot honour the request.
    """

    content: str
    tier: MemoryTier
    subject: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, now: float, retention_days: int | None) -> bool:
        if retention_days is None:
            return False
        return (now - self.created_at) > retention_days * _DAY_SECONDS


@runtime_checkable
class MemoryStore(Protocol):
    """A persistent tier.

    `forget` is part of the protocol rather than an administrative extra. A
    store that can write but not selectively erase is unusable anywhere a
    subject can ask to be removed, and discovering that after the fact means
    rebuilding the store.
    """

    tier: MemoryTier

    async def write(self, record: MemoryRecord) -> None: ...

    async def recall(
        self, query: str | None = None, subject: str | None = None, limit: int = 10
    ) -> list[MemoryRecord]: ...

    async def forget(self, subject: str) -> int: ...

    async def prune(self) -> int: ...


class WorkingMemory:
    """This run's scratch space. Bounded, and dies with the run.

    Bounded because unbounded working memory is how a long run walks into a
    context-window failure at step forty, having spent the budget getting
    there. Eviction is oldest-first: the newest observations are the ones the
    next decision depends on.

    Deliberately not a `MemoryStore` — it has no retention policy, no subject
    index and no persistence, and pretending otherwise would invite someone to
    swap a durable store in and quietly create a record nobody is managing.
    """

    def __init__(self, max_items: int = 50) -> None:
        if max_items < 1:
            raise ValueError("working memory needs room for at least one item")
        self.max_items = max_items
        self._items: deque[MemoryRecord] = deque(maxlen=max_items)
        self._evicted = 0

    def add(self, content: str, **metadata: Any) -> MemoryRecord:
        if len(self._items) == self.max_items:
            self._evicted += 1
        record = MemoryRecord(content=content, tier=MemoryTier.WORKING, metadata=metadata)
        self._items.append(record)
        return record

    @property
    def items(self) -> tuple[MemoryRecord, ...]:
        return tuple(self._items)

    @property
    def evicted_count(self) -> int:
        """How much has been dropped.

        Exposed rather than hidden: silent eviction looks exactly like a model
        ignoring its context, and the two are debugged very differently.
        """
        return self._evicted

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


class InMemoryStore:
    """Reference implementation. Correct, not durable.

    Substring matching stands in for whatever retrieval the real store uses.
    The point of shipping it is that a service can exercise the tier
    boundaries, the retention policy and the erasure path before choosing a
    database.
    """

    def __init__(
        self,
        tier: MemoryTier,
        retention_days: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if tier is MemoryTier.WORKING:
            raise ValueError("working memory is not a store; use WorkingMemory")
        # Annotated explicitly: the guard above narrows `tier` to a literal
        # union, and a narrower mutable attribute does not satisfy the
        # protocol's `tier: MemoryTier`.
        self.tier: MemoryTier = tier
        self.retention_days = retention_days
        self._clock = clock
        self._records: list[MemoryRecord] = []

    async def write(self, record: MemoryRecord) -> None:
        if record.tier is not self.tier:
            # A record written to the wrong tier inherits the wrong retention
            # policy, which is how episodic data ends up living forever in a
            # semantic store.
            raise ValueError(f"cannot write a {record.tier} record to a {self.tier} store")
        self._records.append(record)

    async def recall(
        self, query: str | None = None, subject: str | None = None, limit: int = 10
    ) -> list[MemoryRecord]:
        now = self._clock()
        matches = [
            r
            for r in self._records
            if not r.is_expired(now, self.retention_days)
            and (subject is None or r.subject == subject)
            and (query is None or query.lower() in r.content.lower())
        ]
        # Newest first: recency is the closest thing to relevance that a store
        # without embeddings can honestly offer.
        matches.sort(key=lambda r: r.created_at, reverse=True)
        return matches[:limit]

    async def forget(self, subject: str) -> int:
        before = len(self._records)
        self._records = [r for r in self._records if r.subject != subject]
        return before - len(self._records)

    async def prune(self) -> int:
        """Drop what retention no longer permits us to keep.

        Expired records are filtered out of `recall` as well, so retention is
        honoured even if nobody ever runs this. Pruning reclaims the space;
        the filter is what makes the guarantee.
        """
        if self.retention_days is None:
            return 0
        now = self._clock()
        before = len(self._records)
        self._records = [r for r in self._records if not r.is_expired(now, self.retention_days)]
        return before - len(self._records)

    def __len__(self) -> int:
        return len(self._records)


class Memory:
    """The four tiers, together.

    A facade rather than a base class, so a service can supply a different
    backend per tier — episodic in Postgres because it must be erasable and
    auditable, semantic in a vector store because it is searched by meaning.
    Those are different products for good reasons.
    """

    def __init__(
        self,
        episodic: MemoryStore | None = None,
        semantic: MemoryStore | None = None,
        procedural: MemoryStore | None = None,
        working: WorkingMemory | None = None,
    ) -> None:
        # `x or default` is wrong here and was the first implementation.
        # These classes define __len__, so an *empty* store is falsy, and a
        # perfectly valid store handed in by a caller was silently discarded
        # for the default — including its retention policy. The bug is
        # invisible until the store has something in it, by which point the
        # data has been written under the wrong retention.
        self.working: WorkingMemory = working if working is not None else WorkingMemory()
        self.episodic: MemoryStore = (
            episodic
            if episodic is not None
            else InMemoryStore(MemoryTier.EPISODIC, retention_days=365)
        )
        self.semantic: MemoryStore = (
            semantic if semantic is not None else InMemoryStore(MemoryTier.SEMANTIC)
        )
        self.procedural: MemoryStore = (
            procedural if procedural is not None else InMemoryStore(MemoryTier.PROCEDURAL)
        )

    def _stores(self) -> Iterable[MemoryStore]:
        return (self.episodic, self.semantic, self.procedural)

    # ------------------------------------------------------------- writing
    async def remember(
        self,
        content: str,
        tier: MemoryTier,
        subject: str | None = None,
        tags: Sequence[str] = (),
        **metadata: Any,
    ) -> MemoryRecord:
        record = MemoryRecord(
            content=content,
            tier=tier,
            subject=subject,
            tags=tuple(tags),
            metadata=metadata,
        )
        if tier is MemoryTier.WORKING:
            self.working.add(content, **metadata)
            return record
        await getattr(self, tier.value).write(record)
        return record

    async def remember_trajectory(self, goal: str, result: RunResult) -> MemoryRecord | None:
        """Record a successful tool sequence as procedural memory.

        Only successes. A failed trajectory is worth investigating and is not
        worth repeating, and a store of both is a store an agent cannot learn
        from without first learning which is which.
        """
        if not result.succeeded:
            return None
        sequence = [
            s.tool_name for s in result.steps if s.kind is StepKind.TOOL_CALL and s.tool_name
        ]
        if not sequence:
            return None
        return await self.remember(
            content=" -> ".join(sequence),
            tier=MemoryTier.PROCEDURAL,
            tags=("trajectory",),
            goal=goal,
            steps=len(result.steps),
            cost_usd=result.cost_usd,
        )

    # ------------------------------------------------------------- reading
    async def recall(
        self,
        query: str | None = None,
        subject: str | None = None,
        tiers: Sequence[MemoryTier] | None = None,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        wanted = tuple(tiers or (MemoryTier.EPISODIC, MemoryTier.SEMANTIC, MemoryTier.PROCEDURAL))
        out: list[MemoryRecord] = []
        for store in self._stores():
            if store.tier in wanted:
                out.extend(await store.recall(query=query, subject=subject, limit=limit))
        out.sort(key=lambda r: r.created_at, reverse=True)
        return out[:limit]

    # ------------------------------------------------------------ erasure
    async def forget_subject(self, subject: str) -> dict[str, int]:
        """Erase everything held about a subject, across every tier.

        Returns a per-tier count, because "we deleted it" is not an answer
        anybody accepts and "we deleted 14 episodic and 2 semantic records" is.

        Working memory is cleared too: a run in flight holding the subject in
        context is still holding it.
        """
        counts = {store.tier.value: await store.forget(subject) for store in self._stores()}
        counts[MemoryTier.WORKING.value] = len(self.working)
        self.working.clear()
        return counts

    async def prune_expired(self) -> dict[str, int]:
        return {store.tier.value: await store.prune() for store in self._stores()}
