"""Memory tiers.

Most of these test the boundaries between tiers rather than storage, because
the boundaries are the design. A single store would pass a storage test and
fail every one of these.
"""

from __future__ import annotations

import pytest

from harness import (
    InMemoryStore,
    Memory,
    MemoryRecord,
    MemoryStore,
    MemoryTier,
    Principal,
    RunOutcome,
    RunResult,
    WorkingMemory,
)
from harness.run import StepKind, StepRecord

P = Principal(id="u1")


class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance_days(self, days: float) -> None:
        self.now += days * 86_400.0


# --------------------------------------------------------------- working
def test_working_memory_is_bounded():
    """Unbounded working memory is how a long run walks into a context failure."""
    w = WorkingMemory(max_items=3)
    for i in range(10):
        w.add(f"item {i}")
    assert len(w) == 3
    assert [r.content for r in w.items] == ["item 7", "item 8", "item 9"]


def test_eviction_is_counted_not_silent():
    """Silent eviction looks exactly like a model ignoring its context."""
    w = WorkingMemory(max_items=2)
    for i in range(5):
        w.add(f"i{i}")
    assert w.evicted_count == 3


def test_working_memory_needs_room_for_something():
    with pytest.raises(ValueError):
        WorkingMemory(max_items=0)


def test_working_memory_clears():
    w = WorkingMemory()
    w.add("x")
    w.clear()
    assert len(w) == 0


# ----------------------------------------------------------- tier boundaries
async def test_a_record_cannot_be_written_to_the_wrong_tier():
    """Wrong tier means wrong retention policy — that is how episodic data lives forever."""
    store = InMemoryStore(MemoryTier.SEMANTIC)
    episodic = MemoryRecord(content="x", tier=MemoryTier.EPISODIC)
    with pytest.raises(ValueError, match="cannot write"):
        await store.write(episodic)


def test_working_memory_is_not_a_store():
    """Allowing it would invite swapping in a durable backend nobody is managing."""
    with pytest.raises(ValueError, match="not a store"):
        InMemoryStore(MemoryTier.WORKING)


def test_the_reference_store_satisfies_the_protocol():
    assert isinstance(InMemoryStore(MemoryTier.SEMANTIC), MemoryStore)


# --------------------------------------------------------------- retention
async def test_expired_records_are_not_recalled_even_without_pruning():
    """Retention is honoured by the read path, so it holds even if nobody prunes."""
    clock = _Clock()
    store = InMemoryStore(MemoryTier.EPISODIC, retention_days=30, clock=clock)
    await store.write(MemoryRecord(content="old", tier=MemoryTier.EPISODIC, created_at=clock.now))

    assert len(await store.recall()) == 1
    clock.advance_days(31)
    assert await store.recall() == []


async def test_pruning_reclaims_the_space():
    clock = _Clock()
    store = InMemoryStore(MemoryTier.EPISODIC, retention_days=30, clock=clock)
    await store.write(MemoryRecord(content="old", tier=MemoryTier.EPISODIC, created_at=clock.now))
    clock.advance_days(31)

    assert len(store) == 1
    assert await store.prune() == 1
    assert len(store) == 0


async def test_no_retention_means_indefinite():
    clock = _Clock()
    store = InMemoryStore(MemoryTier.SEMANTIC, retention_days=None, clock=clock)
    await store.write(MemoryRecord(content="fact", tier=MemoryTier.SEMANTIC, created_at=clock.now))
    clock.advance_days(9999)
    assert len(await store.recall()) == 1


async def test_tiers_can_carry_different_retention():
    """The whole reason they are separate stores."""
    m = Memory(
        episodic=InMemoryStore(MemoryTier.EPISODIC, retention_days=30),
        semantic=InMemoryStore(MemoryTier.SEMANTIC, retention_days=None),
    )
    assert m.episodic.retention_days == 30
    assert m.semantic.retention_days is None


# ----------------------------------------------------------------- erasure
async def test_forgetting_a_subject_clears_every_tier_and_reports_counts():
    """'We deleted it' is not an answer anyone accepts."""
    m = Memory()
    await m.remember("saw the file", MemoryTier.EPISODIC, subject="consumer-42")
    await m.remember("called again", MemoryTier.EPISODIC, subject="consumer-42")
    await m.remember("lives in GA", MemoryTier.SEMANTIC, subject="consumer-42")
    await m.remember("unrelated fact", MemoryTier.SEMANTIC, subject="consumer-99")
    m.working.add("consumer-42 in context")

    counts = await m.forget_subject("consumer-42")

    assert counts["episodic"] == 2
    assert counts["semantic"] == 1
    assert counts["working"] == 1
    assert await m.recall(subject="consumer-42") == []


async def test_forgetting_leaves_other_subjects_alone():
    m = Memory()
    await m.remember("a", MemoryTier.EPISODIC, subject="s1")
    await m.remember("b", MemoryTier.EPISODIC, subject="s2")
    await m.forget_subject("s1")
    assert len(await m.recall(subject="s2")) == 1


async def test_working_memory_is_cleared_on_erasure():
    """A run in flight holding the subject in context is still holding it."""
    m = Memory()
    m.working.add("consumer-42 said ...")
    await m.forget_subject("consumer-42")
    assert len(m.working) == 0


# -------------------------------------------------------------- procedural
def _result(*tools: str, outcome: RunOutcome = RunOutcome.COMPLETED) -> RunResult:
    steps = tuple(
        StepRecord(index=i, kind=StepKind.TOOL_CALL, summary=t, tool_name=t)
        for i, t in enumerate(tools)
    )
    return RunResult(run_id="r", outcome=outcome, steps=steps, cost_usd=0.05)


async def test_a_successful_trajectory_is_remembered():
    m = Memory()
    rec = await m.remember_trajectory("check a file", _result("search", "summarise"))
    assert rec is not None
    assert rec.content == "search -> summarise"
    assert rec.tier is MemoryTier.PROCEDURAL


async def test_a_failed_trajectory_is_not_remembered():
    """A store of successes and failures is one an agent cannot learn from."""
    m = Memory()
    rec = await m.remember_trajectory("x", _result("search", outcome=RunOutcome.GAVE_UP))
    assert rec is None
    assert len(await m.recall(tiers=[MemoryTier.PROCEDURAL])) == 0


async def test_a_run_with_no_tool_calls_has_no_trajectory():
    m = Memory()
    assert await m.remember_trajectory("x", _result()) is None


# ------------------------------------------------------------------ recall
async def test_recall_can_be_scoped_to_tiers():
    m = Memory()
    await m.remember("episodic thing", MemoryTier.EPISODIC)
    await m.remember("semantic thing", MemoryTier.SEMANTIC)

    only_semantic = await m.recall(tiers=[MemoryTier.SEMANTIC])
    assert [r.content for r in only_semantic] == ["semantic thing"]


async def test_recall_filters_by_query():
    m = Memory()
    await m.remember("mortgage rates rose", MemoryTier.SEMANTIC)
    await m.remember("the office moved", MemoryTier.SEMANTIC)
    assert len(await m.recall(query="mortgage")) == 1


async def test_recall_respects_the_limit():
    m = Memory()
    for i in range(20):
        await m.remember(f"fact {i}", MemoryTier.SEMANTIC)
    assert len(await m.recall(limit=5)) == 5


async def test_remembering_into_working_does_not_reach_a_store():
    m = Memory()
    await m.remember("scratch", MemoryTier.WORKING)
    assert len(m.working) == 1
    assert await m.recall() == []


# --------------------------------------------- regression: falsy empty stores
async def test_an_empty_store_passed_in_is_not_replaced_by_the_default():
    """Regression.

    `Memory` used `episodic or InMemoryStore(...)`. These classes define
    __len__, so an empty store is falsy and a caller's store — with its
    retention policy — was silently swapped for the default. Invisible until
    the store had records in it, by which point they were written under the
    wrong retention.
    """
    mine = InMemoryStore(MemoryTier.EPISODIC, retention_days=7)
    m = Memory(episodic=mine)
    assert m.episodic is mine
    assert m.episodic.retention_days == 7


def test_an_empty_working_memory_passed_in_is_not_replaced():
    mine = WorkingMemory(max_items=3)
    assert Memory(working=mine).working is mine


async def test_a_non_empty_store_was_never_affected():
    """Which is exactly why the bug survived the first pass."""
    mine = InMemoryStore(MemoryTier.SEMANTIC, retention_days=7)
    await mine.write(MemoryRecord(content="x", tier=MemoryTier.SEMANTIC))
    assert Memory(semantic=mine).semantic is mine
