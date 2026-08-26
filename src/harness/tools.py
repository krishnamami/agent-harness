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
from dataclasses import dataclass, field
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
    idempotent: bool = False
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a tool needs a name")
        if self.parameters.get("type") != "object":
            # Tool-calling APIs universally expect an object schema at the top
            # level. Catching it here beats discovering it at inference time.
            raise ValueError(f"{self.name}: parameters must be a JSON Schema object")


@runtime_checkable
class Tool(Protocol):
    spec: ToolSpec

    async def __call__(self, arguments: dict[str, Any]) -> Any: ...


@dataclass
class _Registered:
    spec: ToolSpec
    fn: ToolFn
    _calls: list[float] = field(default_factory=list)


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


class ToolRegistry:
    """Holds tools, and enforces policy on the way to them.

    Every call passes through `invoke`. There is deliberately no way to reach
    the underlying function without going through the registry — a bypass
    route is how audit trails develop holes.
    """

    def __init__(self, authorization: AuthorizationPolicy | None = None) -> None:
        self._tools: dict[str, _Registered] = {}
        self._authorization = authorization or OpenAuthorization()

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
        """
        if name not in self._tools:
            raise ToolNotFoundError(name)
        entry = self._tools[name]

        decision = self._authorization.authorize(principal, name, arguments)
        if not decision.allowed:
            raise ToolDeniedError(name, decision.reason or "denied by policy")

        self._check_rate_limit(entry)
        return entry.spec

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        """Run a tool that has already passed `check`.

        Deliberately takes no principal: it performs no policy of its own, and
        a signature that suggested otherwise would invite someone to call it
        directly. The only legitimate caller is one that has just checked.
        """
        if name not in self._tools:
            raise ToolNotFoundError(name)
        return await self._tools[name].fn(arguments)

    async def invoke(self, name: str, arguments: dict[str, Any], principal: Principal) -> Any:
        """Check and call. Convenience for callers with nothing to do between."""
        self.check(name, arguments, principal)
        return await self.call(name, arguments)

    def _check_rate_limit(self, entry: _Registered) -> None:
        limit = entry.spec.rate_limit_per_minute
        if limit is None:
            return
        now = time.monotonic()
        entry._calls = [t for t in entry._calls if now - t < 60.0]
        if len(entry._calls) >= limit:
            raise RateLimitExceededError(entry.spec.name, limit)
        entry._calls.append(now)
