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

from harness.delegation import PrivilegeEscalationError, delegate, new_child_context
from harness.executor import run_agent
from harness.gates import (
    ApprovalDecision,
    ApprovalGate,
    ApprovalRequest,
    AutoApprove,
    RecordingGate,
    RefuseAll,
    TierGate,
)
from harness.mcp import RemoteToolError, Transport, load_tools
from harness.memory import (
    InMemoryStore,
    Memory,
    MemoryRecord,
    MemoryStore,
    MemoryTier,
    WorkingMemory,
)
from harness.overlays import (
    FourEyesGate,
    PurposeAuthorization,
    RegulatedAudit,
    regulated_overlay,
)
from harness.planner import (
    CallTool,
    CallTools,
    Decision,
    Finish,
    Planner,
    PlannerState,
    ScriptedPlanner,
)
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
    SubRun,
    WallClockExceededError,
)
from harness.tools import (
    InProcessRateLimiter,
    RateLimiter,
    RateLimitExceededError,
    Tool,
    ToolArgumentError,
    ToolDeniedError,
    ToolNotFoundError,
    ToolRegistry,
    ToolSpec,
)
from harness.trace import (
    Divergence,
    Provenance,
    ReplayReport,
    RunTrace,
    TraceError,
    record_trace,
    replay,
)

__all__ = [
    "ApprovalDecision",
    "ApprovalGate",
    "ApprovalRequest",
    "AuditPolicy",
    "AuthorizationDecision",
    "AuthorizationPolicy",
    "AutoApprove",
    "CallTool",
    "CallTools",
    "CostLimitExceededError",
    "Decision",
    "Divergence",
    "Finish",
    "FourEyesGate",
    "InMemoryStore",
    "InProcessRateLimiter",
    "LimitExceededError",
    "Memory",
    "MemoryRecord",
    "MemoryStore",
    "MemoryTier",
    "OpenAuthorization",
    "Planner",
    "PlannerState",
    "Principal",
    "PrivilegeEscalationError",
    "Provenance",
    "PurposeAuthorization",
    "RateLimitExceededError",
    "RateLimiter",
    "RecordingGate",
    "RefuseAll",
    "RegulatedAudit",
    "RemoteToolError",
    "ReplayReport",
    "RiskTier",
    "RoleBasedAuthorization",
    "RunContext",
    "RunLimits",
    "RunOutcome",
    "RunResult",
    "RunTrace",
    "ScriptedPlanner",
    "StandardAudit",
    "StepKind",
    "StepLimitExceededError",
    "StepRecord",
    "SubRun",
    "TierGate",
    "Tool",
    "ToolArgumentError",
    "ToolDeniedError",
    "ToolNotFoundError",
    "ToolRegistry",
    "ToolSpec",
    "TraceError",
    "Transport",
    "WallClockExceededError",
    "WorkingMemory",
    "delegate",
    "load_tools",
    "new_child_context",
    "record_trace",
    "regulated_overlay",
    "replay",
    "run_agent",
]
