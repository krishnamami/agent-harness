"""The rate limiter is a budget, and budgets are spent by work.

The defect these guard against shipped in v1.0.0 and was invisible: `check()`
consumed rate-limit budget, and `ReplayRegistry` inherits `check`, so replaying
a trace from March spent today's quota. Nothing failed until enough replays ran,
and then an audit failed for a reason that had nothing to do with the run being
audited. See ADR-0017.
"""

from __future__ import annotations

import pytest

from harness import (
    InProcessRateLimiter,
    Principal,
    RateLimiter,
    RateLimitExceededError,
    RiskTier,
    ToolRegistry,
    ToolSpec,
)

SCHEMA = {"type": "object", "properties": {}}


async def _ok(arguments: dict[str, object]) -> str:
    return "ok"


def _spec(name: str = "t", limit: int | None = None) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="d",
        parameters=SCHEMA,
        tier=RiskTier.ROUTINE,
        rate_limit_per_minute=limit,
    )


def _registry(limit: int | None = None, limiter: RateLimiter | None = None) -> ToolRegistry:
    registry = ToolRegistry(rate_limiter=limiter)
    registry.register(_spec(limit=limit), _ok)
    return registry


class _AlwaysDeny:
    """Substitutable by construction: if the protocol is real, this works."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, int]] = []

    async def allow(self, tool_name: str, limit_per_minute: int) -> bool:
        self.asked.append((tool_name, limit_per_minute))
        return False


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# ------------------------------------------------------- the replay defect


def test_check_does_not_consume_budget() -> None:
    """The one that matters. A dry run must not spend what a real call needs."""
    registry = _registry(limit=1)
    principal = Principal(id="agent")
    for _ in range(100):
        registry.check("t", {}, principal)


@pytest.mark.asyncio
async def test_call_still_has_its_budget_after_many_checks() -> None:
    registry = _registry(limit=1)
    principal = Principal(id="agent")
    for _ in range(100):
        registry.check("t", {}, principal)
    assert await registry.call("t", {}) == "ok"


@pytest.mark.asyncio
async def test_replay_style_override_costs_nothing() -> None:
    """ReplayRegistry overrides `call` only. Budget must live there, not in
    `check`, or a replay spends quota it has no business spending."""

    class ReplayLike(ToolRegistry):
        async def call(self, name: str, arguments: dict[str, object]) -> object:
            return "replayed"

    registry = ReplayLike(rate_limiter=_AlwaysDeny())
    registry.register(_spec(limit=1), _ok)
    for _ in range(10):
        assert await registry.call("t", {}) == "replayed"


# ------------------------------------------------------------ the limit itself


@pytest.mark.asyncio
async def test_limit_bites_on_the_call_after_the_budget() -> None:
    registry = _registry(limit=2)
    await registry.call("t", {})
    await registry.call("t", {})
    with pytest.raises(RateLimitExceededError):
        await registry.call("t", {})


@pytest.mark.asyncio
async def test_budget_recovers_after_the_window() -> None:
    clock = _FakeClock()
    registry = _registry(limit=1, limiter=InProcessRateLimiter(clock=clock))
    await registry.call("t", {})
    with pytest.raises(RateLimitExceededError):
        await registry.call("t", {})
    clock.now += 61.0
    assert await registry.call("t", {}) == "ok"


@pytest.mark.asyncio
async def test_a_tool_with_no_declared_limit_is_never_asked() -> None:
    limiter = _AlwaysDeny()
    registry = ToolRegistry(rate_limiter=limiter)
    registry.register(_spec("u"), _ok)
    assert await registry.call("u", {}) == "ok"
    assert limiter.asked == []


@pytest.mark.asyncio
async def test_limits_are_counted_per_tool_not_globally() -> None:
    registry = ToolRegistry()
    registry.register(_spec("a", limit=1), _ok)
    registry.register(_spec("b", limit=1), _ok)
    await registry.call("a", {})
    assert await registry.call("b", {}) == "ok"


# --------------------------------------------------------- the contract itself


def test_injected_limiter_satisfies_the_protocol() -> None:
    assert isinstance(_AlwaysDeny(), RateLimiter)
    assert isinstance(InProcessRateLimiter(), RateLimiter)


@pytest.mark.asyncio
async def test_injected_limiter_is_actually_consulted() -> None:
    """Extracting a protocol nothing substitutes would be ceremony."""
    limiter = _AlwaysDeny()
    registry = _registry(limit=99, limiter=limiter)
    with pytest.raises(RateLimitExceededError):
        await registry.call("t", {})
    assert limiter.asked == [("t", 99)]


@pytest.mark.asyncio
async def test_default_is_in_process() -> None:
    """The default is correct for one replica and wrong for two, which is a
    thing a reader should be able to discover from the type."""
    registry = ToolRegistry()
    assert isinstance(registry._rate_limiter, InProcessRateLimiter)
