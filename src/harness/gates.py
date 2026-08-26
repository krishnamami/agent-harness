"""Human-in-the-loop, keyed to risk tier.

Blanket human review is the junior answer. It is also self-defeating: a gate
that engages on every call gets switched off within a fortnight, and a
switched-off gate protects nothing. The useful question is not *whether* a
human reviews but *which* calls a human reviews, and the answer is a function
of how bad it is to be wrong.

So a gate has a threshold. Below it, nothing happens. At or above it, a human
— or whatever stands in for one — decides.

Two properties of that decision are worth being precise about:

- **A refusal is terminal.** Unlike an authorisation denial, which the planner
  sees and may route around, a human saying no is not an obstacle to work
  around. Feeding it back as an observation would produce an agent that
  rephrases its request until someone says yes.
- **The approval is recorded in the trace.** Who approved what, and when. An
  approval that leaves no record is indistinguishable from no approval at all,
  eighteen months later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from harness.policy import Principal, RiskTier


@dataclass(frozen=True)
class ApprovalRequest:
    """What a reviewer is being asked to approve.

    Carries the rationale and the steps taken so far, because "may this agent
    call this tool" is not answerable without knowing why it wants to and what
    it has already done.
    """

    run_id: str
    principal: Principal
    tool: str
    arguments: dict[str, Any]
    tier: RiskTier
    rationale: str = ""
    steps_taken: int = 0


@dataclass(frozen=True)
class ApprovalDecision:
    """The outcome of a review.

    `gated` says whether a review actually happened. It is explicit rather
    than inferred, because "approved" and "nobody looked" are very different
    facts and only one of them belongs in an audit trail. Recording an
    approval step for every ungated call would pad every routine run with
    entries that assert an oversight nobody performed.
    """

    approved: bool
    approver: str
    reason: str | None = None
    gated: bool = True
    metadata: dict[str, str] = field(default_factory=dict)

    @classmethod
    def approve(
        cls, approver: str, reason: str | None = None, gated: bool = True
    ) -> ApprovalDecision:
        return cls(approved=True, approver=approver, reason=reason, gated=gated)

    @classmethod
    def not_gated(cls, reason: str) -> ApprovalDecision:
        """No review was required. Not the same as approval."""
        return cls(approved=True, approver="not-gated", reason=reason, gated=False)

    @classmethod
    def refuse(cls, approver: str, reason: str) -> ApprovalDecision:
        return cls(approved=False, approver=approver, reason=reason)


@runtime_checkable
class ApprovalGate(Protocol):
    """Decides whether a call at or above the threshold may proceed."""

    name: str

    async def review(self, request: ApprovalRequest) -> ApprovalDecision: ...


class AutoApprove:
    """Approves everything. The neutral default, and never a control.

    Named so that nobody reads a configuration and believes a gate is in place.
    A deployment that wants no gate should be visibly using this rather than
    passing nothing.
    """

    name = "auto-approve"

    def __init__(self, approver: str = "auto") -> None:
        self._approver = approver

    async def review(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision.not_gated("no gate configured")


class TierGate:
    """Engages an inner gate at or above a threshold; waves through below it.

    This is what makes oversight proportionate. Routine work is untouched, so
    the gate keeps its credibility for the calls that matter.
    """

    def __init__(self, inner: ApprovalGate, minimum_tier: RiskTier) -> None:
        self._inner = inner
        self.minimum_tier = minimum_tier
        self.name = f"tier>={minimum_tier.name.lower()}:{inner.name}"

    def engages_for(self, tier: RiskTier) -> bool:
        return tier >= self.minimum_tier

    async def review(self, request: ApprovalRequest) -> ApprovalDecision:
        if not self.engages_for(request.tier):
            return ApprovalDecision.not_gated("below the gate threshold")
        return await self._inner.review(request)


class RefuseAll:
    """Refuses everything at or above the threshold it is wrapped in.

    For a lockdown, and for testing that a refusal actually stops a run.
    """

    name = "refuse-all"

    def __init__(self, approver: str = "lockdown", reason: str = "reviews are suspended") -> None:
        self._approver = approver
        self._reason = reason

    async def review(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision.refuse(self._approver, self._reason)


class RecordingGate:
    """Wraps a gate and remembers what it was asked.

    Useful in tests, and useful in production for the same reason: the set of
    calls that reached a human is itself a signal about whether the threshold
    is set in the right place.
    """

    def __init__(self, inner: ApprovalGate) -> None:
        self._inner = inner
        self.name = f"recording:{inner.name}"
        self.requests: list[ApprovalRequest] = []
        self.decisions: list[ApprovalDecision] = []

    async def review(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        decision = await self._inner.review(request)
        self.decisions.append(decision)
        return decision
