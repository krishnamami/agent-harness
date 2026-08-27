"""Tools the harness did not define.

Everything else in this package assumes a deployment registers its own tools by
hand. That works while the tools and the agent are written by the same team,
and stops working the moment a tool registry is a service — which is the point
at which "a hundred agents in an enterprise" starts being the actual problem.

This turns a served MCP tool surface into registered `ToolSpec`s. What it is
careful about is the *risk metadata*, because that is what the approval gate
keys on. The tools come from somewhere else; the tiers must too, and a tier the
harness cannot read is a tier the harness must not guess downward. See ADR-0019.

No transport ships here. `Transport` is a protocol with one method, and stdio,
HTTP or an in-process double all satisfy it — same posture as `MemoryStore`,
`RateLimiter` and `ApprovalGate`. A harness that also owned a socket would be
two products.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from harness.policy import RiskTier
from harness.tools import ToolDeniedError, ToolFn, ToolRegistry, ToolSpec

TIERS = {
    "routine": RiskTier.ROUTINE,
    "elevated": RiskTier.ELEVATED,
    "consequential": RiskTier.CONSEQUENTIAL,
    "critical": RiskTier.CRITICAL,
}


@runtime_checkable
class Transport(Protocol):
    """One JSON-RPC round trip. Whatever carries it is not our business."""

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


class RemoteToolError(Exception):
    """The remote tool ran and the action failed.

    Distinct from `ToolDeniedError`, which means the call was refused before
    anything happened. The difference is the whole question an operator asks
    first — did anything change out there — so it must not be flattened into
    one exception type. Carries the action record id when the server returned
    one, because that is what a compensating call has to name.
    """

    def __init__(self, tool_name: str, reason: str, action_record_id: str | None = None) -> None:
        self.tool_name = tool_name
        self.reason = reason
        self.action_record_id = action_record_id
        detail = f" ({action_record_id})" if action_record_id else ""
        super().__init__(f"{tool_name}: {reason}{detail}")


def _tier(meta: dict[str, Any], fallback: RiskTier) -> RiskTier:
    """Read the served tier, and refuse to invent one.

    A tool arriving without readable risk metadata is not routine. It is
    *unknown*, and the harness has no basis for treating unknown as safe: the
    gate would wave it through and a consequential action would execute
    unreviewed. Unknown resolves to the fallback, which defaults to
    CONSEQUENTIAL — over-gating is a nuisance, under-gating is an incident.
    """
    declared = meta.get("risk_tier")
    if not isinstance(declared, str):
        return fallback
    return TIERS.get(declared.lower(), fallback)


async def load_tools(
    transport: Transport,
    registry: ToolRegistry,
    *,
    unknown_tier: RiskTier = RiskTier.CONSEQUENTIAL,
    rate_limits: dict[str, int] | None = None,
) -> tuple[str, ...]:
    """Register every tool the server serves. Returns the names, in order.

    `rate_limits` are the *local* ones — the harness's own budget, which exists
    so a runaway loop is stopped in-process rather than by a remote refusal a
    model has to interpret. The server has its own and they are the ones that
    actually bind; these are a courtesy to the loop, not a control.
    """
    served = await transport.request("tools/list", {})
    limits = rate_limits or {}
    names: list[str] = []

    for tool in served.get("tools", []):
        name = tool["name"]
        spec = ToolSpec(
            name=name,
            description=tool.get("description", ""),
            # The server's schema, unedited. Rewriting it locally would mean
            # the harness validated against a contract the server does not
            # hold, and the disagreement would surface as an unexplained
            # remote refusal.
            parameters=tool["inputSchema"],
            tier=_tier(tool.get("_meta") or {}, unknown_tier),
            rate_limit_per_minute=limits.get(name),
        )
        registry.register(spec, _caller(transport, name))
        names.append(name)

    return tuple(names)


def _caller(transport: Transport, name: str) -> ToolFn:
    async def call(arguments: dict[str, Any]) -> Any:
        result = await transport.request("tools/call", {"name": name, "arguments": arguments})
        if not result.get("isError"):
            return result.get("structuredContent", result)

        text = _text(result)
        structured = result.get("structuredContent") or {}
        if structured.get("state") == "failed":
            raise RemoteToolError(name, text, structured.get("action_record_id"))
        # Everything else the server refuses -- bad arguments, no entitlement,
        # rate limit -- is a denial. The executor already feeds denials back to
        # the planner as observations rather than ending the run, which is
        # exactly right here: "this needs a duplicate_assessment that reached
        # BLOCK" is something an agent can act on.
        raise ToolDeniedError(name, text)

    return call


def _text(result: dict[str, Any]) -> str:
    parts = [
        block.get("text", "") for block in result.get("content", []) if block.get("type") == "text"
    ]
    return "; ".join(p for p in parts if p) or "refused without a reason"
