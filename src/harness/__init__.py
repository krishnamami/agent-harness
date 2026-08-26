"""Enterprise agent harness.

A harness is not an agent. It is the thing agents run inside, the way an
application server is the thing web applications run inside.

One agent script is easy. A hundred agents inside an enterprise — calling real
systems, spending real money, touching regulated data — is a different problem,
and something has to answer: which tools may this agent call, on whose
authority, how much may it spend, what stops it looping, and how do you
reconstruct what it did eighteen months from now.

Built on ai-golden-path@v1.0.0, which supplies configuration, structured
logging, correlation ids, tracing, the error contract and the CI gate. See
docs/adr/0001.
"""

from harness.policy import (
    AuditPolicy,
    AuthorizationDecision,
    AuthorizationPolicy,
    OpenAuthorization,
    Principal,
    RiskTier,
    RoleBasedAuthorization,
    StandardAudit,
)
from harness.run import (
    CostLimitExceededError,
    LimitExceededError,
    RunContext,
    RunLimits,
    RunOutcome,
    RunResult,
    StepKind,
    StepLimitExceededError,
    StepRecord,
)
from harness.tools import (
    RateLimitExceededError,
    Tool,
    ToolDeniedError,
    ToolNotFoundError,
    ToolRegistry,
    ToolSpec,
)

__all__ = [
    "AuditPolicy",
    "AuthorizationDecision",
    "AuthorizationPolicy",
    "CostLimitExceededError",
    "LimitExceededError",
    "OpenAuthorization",
    "Principal",
    "RateLimitExceededError",
    "RiskTier",
    "RoleBasedAuthorization",
    "RunContext",
    "RunLimits",
    "RunOutcome",
    "RunResult",
    "StandardAudit",
    "StepKind",
    "StepLimitExceededError",
    "StepRecord",
    "Tool",
    "ToolDeniedError",
    "ToolNotFoundError",
    "ToolRegistry",
    "ToolSpec",
]
