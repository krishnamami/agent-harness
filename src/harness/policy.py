"""Policy contracts.

The three decisions that differ between one enterprise and the next, expressed
as protocols with neutral defaults. A regulated deployment supplies different
implementations of the same protocols — it does not fork the harness.

This is the same move ai-golden-path makes with `Guardrail` and `Evaluator`,
one level up. What counts as authorised, what must be retained, and how much
oversight a run needs are all organisational answers, and a harness that
hard-codes one set of answers is only usable by the organisation it was
written for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar, Protocol, runtime_checkable


class RiskTier(IntEnum):
    """How much oversight a run requires.

    Ordered so comparisons work: a gate configured at TIER_2 engages for
    TIER_2 and everything above it.

    Tiering is what makes governance proportionate rather than paralytic. A
    harness that treats every run as high-risk gets routed around, and then
    you have less control than before you had a policy.
    """

    ROUTINE = 0  # reversible, internal, no consumer data
    ELEVATED = 1  # reversible, but touches sensitive data
    CONSEQUENTIAL = 2  # hard to reverse, or informs a decision about a person
    CRITICAL = 3  # irreversible, or legally consequential


@dataclass(frozen=True)
class Principal:
    """Who a run acts as.

    An agent acts as the requesting principal, not as itself. A service
    account with broad rights is how an agent quietly becomes an
    exfiltration path: an instruction injected into retrieved content
    inherits every permission the account holds.

    `purpose` exists because in some regulated contexts authorisation is not
    only "may this identity read this record" but "may it be read *for this
    reason*". Where that does not apply the field is simply unused.
    """

    id: str
    roles: frozenset[str] = frozenset()
    purpose: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason: str | None = None
    obligations: tuple[str, ...] = ()

    @classmethod
    def allow(cls, *obligations: str) -> AuthorizationDecision:
        """Permit, optionally attaching obligations the caller must honour.

        An obligation is a condition of the permission — "redact field X",
        "record this access" — rather than a separate check. Returning them
        with the decision keeps the policy in one place instead of scattering
        it across every call site.
        """
        return cls(allowed=True, obligations=obligations)

    @classmethod
    def deny(cls, reason: str) -> AuthorizationDecision:
        return cls(allowed=False, reason=reason)


@runtime_checkable
class AuthorizationPolicy(Protocol):
    """Decides whether a principal may invoke a tool.

    Consulted per tool call, never once per run. A workflow that establishes
    authority at the top and then acts freely cannot answer "under what
    authority was this specific record read", which is the question that
    actually gets asked afterwards.
    """

    name: str

    def authorize(
        self, principal: Principal, tool_name: str, arguments: dict[str, object]
    ) -> AuthorizationDecision: ...


@runtime_checkable
class AuditPolicy(Protocol):
    """Decides what a run must retain, and for how long.

    Separate from authorisation because the questions are different:
    authorisation is asked before an action and audit is answered long after
    it. Retention is expressed in days rather than as a boolean because "keep
    forever" and "keep for seven years" are different obligations.
    """

    name: str

    def retention_days(self, tier: RiskTier) -> int: ...

    def must_record_arguments(self, tool_name: str) -> bool: ...


class OpenAuthorization:
    """Permits everything. The default, and never correct in production.

    A permissive default is deliberate: a harness that ships with somebody
    else's authorisation rules invites a team to assume they are covered.
    An obviously-empty policy does not.
    """

    name = "open"

    def authorize(
        self, principal: Principal, tool_name: str, arguments: dict[str, object]
    ) -> AuthorizationDecision:
        return AuthorizationDecision.allow()


class RoleBasedAuthorization:
    """Grants tools by role. A reasonable neutral default.

    `required_roles` maps a tool name to the roles that may call it. A tool
    absent from the mapping is denied — an unlisted tool is far more often an
    oversight than an intentionally public one.
    """

    name = "role-based"

    def __init__(self, required_roles: dict[str, frozenset[str]]) -> None:
        self._required = required_roles

    def authorize(
        self, principal: Principal, tool_name: str, arguments: dict[str, object]
    ) -> AuthorizationDecision:
        needed = self._required.get(tool_name)
        if needed is None:
            return AuthorizationDecision.deny(f"tool {tool_name!r} is not granted to any role")
        if principal.roles & needed:
            return AuthorizationDecision.allow()
        return AuthorizationDecision.deny(f"principal lacks a role permitting {tool_name!r}")


class StandardAudit:
    """Retention that scales with risk tier. A neutral starting point.

    Deliberately not calibrated to any jurisdiction — a regulated deployment
    replaces this with one that is.
    """

    name = "standard"

    _RETENTION: ClassVar[dict[RiskTier, int]] = {
        RiskTier.ROUTINE: 30,
        RiskTier.ELEVATED: 365,
        RiskTier.CONSEQUENTIAL: 365 * 3,
        RiskTier.CRITICAL: 365 * 7,
    }

    def retention_days(self, tier: RiskTier) -> int:
        return self._RETENTION[tier]

    def must_record_arguments(self, tool_name: str) -> bool:
        # Arguments frequently contain the sensitive part of a call. Recording
        # them by default is the safer choice for reconstructability and the
        # riskier one for data minimisation; a real policy decides per tool.
        return True
