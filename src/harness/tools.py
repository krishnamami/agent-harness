"""The tool registry.

A list of Python functions is not a registry. The difference is that a
registry can answer three questions a list cannot: who may call this, how
often, and what does it accept. Those answers are what make a hundred agents
in an enterprise survivable.

Every tool declares a JSON Schema for its arguments. Not for documentation —
so the harness can reject a malformed call before it reaches a real system,
and so a model's tool-use surface is generated from the same source of truth
that validates it.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from harness.policy import AuthorizationPolicy, OpenAuthorization, Principal, RiskTier

ToolFn = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    """Everything the harness needs to know about a tool before calling it."""

    name: str
    description: str
    parameters: dict[str, Any]
    tier: RiskTier = RiskTier.ROUTINE
    rate_limit_per_minute: int | None = None
    # Per-tool, because one global timeout is always wrong for something:
    # a row lookup that takes 30s is broken, and a document extraction that
    # takes 30s is normal. None means the run's default applies.
    timeout_seconds: float | None = None
    idempotent: bool = False
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a tool needs a name")
        if self.parameters.get("type") != "object":
            # Tool-calling APIs universally expect an object schema at the top
            # level. Catching it here beats discovering it at inference time.
            raise ValueError(f"{self.name}: parameters must be a JSON Schema object")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError(f"{self.name}: timeout_seconds must be positive")


@runtime_checkable
class Tool(Protocol):
    spec: ToolSpec

    async def __call__(self, arguments: dict[str, Any]) -> Any: ...


@dataclass
class _Registered:
    spec: ToolSpec
    fn: ToolFn


class ToolDeniedError(Exception):
    """Raised when a policy refuses a call.

    Distinct from a tool failing. A denial is the system working; a failure is
    the system not working, and conflating them makes both harder to
    investigate.
    """

    def __init__(self, tool_name: str, reason: str) -> None:
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"{tool_name}: {reason}")


class ToolNotFoundError(KeyError):
    pass


class RateLimitExceededError(Exception):
    def __init__(self, tool_name: str, limit: int) -> None:
        self.tool_name = tool_name
        self.limit = limit
        super().__init__(f"{tool_name}: exceeded {limit} calls/minute")


@runtime_checkable
class RateLimiter(Protocol):
    """Where a tool's call budget is counted.

    A contract for the same reason `MemoryStore` is one: the reference
    implementation is correct and in-process, and in-process is wrong the
    moment there are two replicas. Three pods each enforcing "sixty a minute"
    permit a hundred and eighty, and the failure is silent -- the limit still
    appears to work, and the downstream system is the one that finds out.

    `allow` both checks and consumes, in that order, and returns whether the
    call may proceed. Splitting them into check-then-record would open a race
    that a shared backend cannot close.
    """

    async def allow(self, tool_name: str, limit_per_minute: int) -> bool: ...


class InProcessRateLimiter:
    """Reference implementation. Correct within one process, and only there.

    Ships so a service can exercise the limit in tests and single-replica
    deployments without standing up Redis. Anything with more than one replica
    needs a shared implementation of the protocol above -- which is a
    deployment decision, not a harness one.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._calls: dict[str, list[float]] = {}

    async def allow(self, tool_name: str, limit_per_minute: int) -> bool:
        now = self._clock()
        recent = [t for t in self._calls.get(tool_name, []) if now - t < 60.0]
        if len(recent) >= limit_per_minute:
            self._calls[tool_name] = recent
            return False
        recent.append(now)
        self._calls[tool_name] = recent
        return True


class ToolRegistry:
    """Holds tools, and enforces policy on the way to them.

    Every call passes through `invoke`. There is deliberately no way to reach
    the underlying function without going through the registry — a bypass
    route is how audit trails develop holes.
    """

    def __init__(
        self,
        authorization: AuthorizationPolicy | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._tools: dict[str, _Registered] = {}
        self._authorization = authorization or OpenAuthorization()
        # Defaults to in-process, which is right for one replica and wrong for
        # two. Naming the default here is what makes that a choice rather than
        # a surprise.
        self._rate_limiter: RateLimiter = rate_limiter or InProcessRateLimiter()

    # ---------------------------------------------------------------- register
    def register(self, spec: ToolSpec, fn: ToolFn) -> None:
        if spec.name in self._tools:
            # Silent replacement would let a later import quietly shadow a
            # tool that policy was written against.
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = _Registered(spec=spec, fn=fn)

    @property
    def authorization(self) -> AuthorizationPolicy:
        """The policy in force. Read-only — swapping it mid-run would mean
        different calls in the same run were judged by different rules."""
        return self._authorization

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def spec(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise ToolNotFoundError(name)
        return self._tools[name].spec

    # ------------------------------------------------------------- describe
    def describe_for(self, principal: Principal) -> list[dict[str, Any]]:
        """The tool surface this principal may actually use.

        Filtered by authorisation rather than returning everything and
        rejecting later. A planner shown tools it cannot call will plan to
        call them, then fail — burning a step and a model call to learn
        something the registry already knew.
        """
        visible: list[dict[str, Any]] = []
        for name, entry in sorted(self._tools.items()):
            if self._authorization.authorize(principal, name, {}).allowed:
                visible.append(
                    {
                        "name": name,
                        "description": entry.spec.description,
                        "parameters": entry.spec.parameters,
                    }
                )
        return visible

    # --------------------------------------------------------------- invoke
    def check(self, name: str, arguments: dict[str, Any], principal: Principal) -> ToolSpec:
        """Everything that must pass before a tool runs -- without running it.

        Separated from `invoke` so that "would this call be permitted" can be
        answered without the side effect. A dry run, a pre-flight check and a
        replay all need the policy without the action, and a replay that
        skipped the policy could not tell you whether a run permitted in March
        would still be permitted today.

        Raises rather than returning a verdict: a caller that ignores a
        returned boolean is a bug that looks like working code.

        Deliberately does NOT consume rate-limit budget. A rate limit is a
        budget, not a permission, and this method is documented as safe for a
        dry run and a replay -- both of which would otherwise burn today's quota
        re-examining a run from March, and eventually fail for a reason that has
        nothing to do with the run being replayed.
        """
        if name not in self._tools:
            raise ToolNotFoundError(name)
        entry = self._tools[name]

        decision = self._authorization.authorize(principal, name, arguments)
        if not decision.allowed:
            raise ToolDeniedError(name, decision.reason or "denied by policy")

        return entry.spec

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        """Run a tool that has already passed `check`.

        Deliberately takes no principal: it performs no AUTHORISATION policy of
        its own, and a signature that suggested otherwise would invite someone
        to call it directly. The only legitimate caller is one that has just
        checked.

        It does consume rate-limit budget, because this is where the tool
        actually runs and a budget should be spent by work rather than by
        asking whether work would be permitted. `ReplayRegistry` overrides this
        method entirely, so a replay costs nothing.

        The trade-off: a call refused for rate rather than policy now fails
        after any approval it needed, instead of being filtered out before it.
        That wastes an approval in the rare case, and it is the price of a rate
        limit that means what it says. See ADR-0017.
        """
        if name not in self._tools:
            raise ToolNotFoundError(name)
        spec = self._tools[name].spec
        limit = spec.rate_limit_per_minute
        if limit is not None and not await self._rate_limiter.allow(name, limit):
            raise RateLimitExceededError(name, limit)
        return await self._tools[name].fn(arguments)

    async def invoke(self, name: str, arguments: dict[str, Any], principal: Principal) -> Any:
        """Check and call. Convenience for callers with nothing to do between."""
        self.check(name, arguments, principal)
        return await self.call(name, arguments)
