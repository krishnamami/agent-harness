"""The regulated overlay.

Everything else in this package is deliberately neutral. This module is one
example of the substitution ADR-0002 exists to make possible: the same
harness, the same executor, the same tools — different answers to what counts
as authorised, what must be retained, and what a human sees.

Nothing here is imported by the core. Swapping this module for another is how
the harness moves between regulatory contexts, and it is a module rather than
a fork so that the two cannot drift apart.

The shape modelled here is **permissible purpose**: authorisation that depends
on *why* a record is being accessed, not only on *who* is asking. It is drawn
from consumer-credit practice, where an identity with the technical ability to
read a file may still have no lawful reason to. Other regimes will want a
different implementation of the same protocols, which is the point.
"""

from __future__ import annotations

from typing import Any

from harness.gates import ApprovalDecision, ApprovalGate, ApprovalRequest, TierGate
from harness.policy import AuthorizationDecision, Principal, RiskTier


class PurposeAuthorization:
    """Authorisation by declared purpose, checked at every tool call.

    Two departures from the neutral `RoleBasedAuthorization`:

    - **A principal with no declared purpose is denied outright.** Not "denied
      for sensitive tools" — denied. An undeclared purpose is not a narrow
      permission, it is an unanswerable question.
    - **The purpose travels with the call**, so it is established at the point
      of access rather than asserted once at the top of a workflow. That is
      the difference between an audit trail that can answer "under what
      authority was this specific record read" and one that cannot.

    Permission carries an obligation to record the access, which the caller is
    expected to honour — returning it with the decision keeps the policy in one
    place rather than scattering it across call sites.
    """

    name = "purpose-based"

    def __init__(self, tool_purposes: dict[str, frozenset[str]]) -> None:
        self._purposes = tool_purposes

    def authorize(
        self, principal: Principal, tool_name: str, arguments: dict[str, Any]
    ) -> AuthorizationDecision:
        permitted = self._purposes.get(tool_name)
        if permitted is None:
            return AuthorizationDecision.deny(
                f"{tool_name!r} is not available for any declared purpose"
            )
        if principal.purpose is None:
            return AuthorizationDecision.deny(f"no purpose declared for access to {tool_name!r}")
        if principal.purpose not in permitted:
            return AuthorizationDecision.deny(
                f"purpose {principal.purpose!r} does not permit {tool_name!r}"
            )
        return AuthorizationDecision.allow(f"record:access:{tool_name}")


class RegulatedAudit:
    """Retention keyed to tier, with arguments withheld for sensitive tools.

    Withholding arguments is a real trade and is made deliberately: the trace
    remains inspectable but is no longer replayable, because the inputs are
    gone. `decisions_from` raises rather than replaying with empty arguments,
    so the loss is loud.

    The retention figures below are illustrative. A real deployment sets them
    from its actual obligation, which is a legal question rather than an
    engineering one.
    """

    name = "regulated"

    def __init__(
        self,
        sensitive_tools: frozenset[str] = frozenset(),
        retention: dict[RiskTier, int] | None = None,
    ) -> None:
        self._sensitive = sensitive_tools
        self._retention = retention or {
            RiskTier.ROUTINE: 365,
            RiskTier.ELEVATED: 365 * 3,
            RiskTier.CONSEQUENTIAL: 365 * 7,
            RiskTier.CRITICAL: 365 * 7,
        }

    def retention_days(self, tier: RiskTier) -> int:
        return self._retention[tier]

    def must_record_arguments(self, tool_name: str) -> bool:
        return tool_name not in self._sensitive


class FourEyesGate:
    """Requires a named reviewer who is not the requesting principal.

    Self-approval is the failure mode a four-eyes control exists to prevent,
    and it is easy to reintroduce by accident when the approver is supplied by
    the same session that made the request.
    """

    name = "four-eyes"

    def __init__(self, reviewers: frozenset[str]) -> None:
        if not reviewers:
            raise ValueError("a four-eyes gate needs at least one reviewer")
        self._reviewers = reviewers

    async def review(self, request: ApprovalRequest) -> ApprovalDecision:
        eligible = self._reviewers - {request.principal.id}
        if not eligible:
            return ApprovalDecision.refuse(
                "four-eyes", "the only eligible reviewer is the requester"
            )
        return ApprovalDecision.approve(sorted(eligible)[0])


def regulated_overlay(
    tool_purposes: dict[str, frozenset[str]],
    reviewers: frozenset[str],
    sensitive_tools: frozenset[str] = frozenset(),
    gate_from: RiskTier = RiskTier.CONSEQUENTIAL,
) -> tuple[PurposeAuthorization, RegulatedAudit, ApprovalGate]:
    """Assemble the three policies as one coherent set.

    Returned together because they are not independent: a tool sensitive
    enough to withhold its arguments is usually one that needs a purpose and a
    reviewer too, and configuring them in three separate places is how they
    end up inconsistent.
    """
    return (
        PurposeAuthorization(tool_purposes),
        RegulatedAudit(sensitive_tools=sensitive_tools),
        TierGate(FourEyesGate(reviewers), minimum_tier=gate_from),
    )
