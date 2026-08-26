"""Traces, and replaying them.

The question a regulated employer actually asks is not "is your agent
accurate". It is: *an agent did something in March; explain it.*

"The model is non-deterministic" does not survive that conversation. Neither
does a pile of log lines, because logs record what was written down, not what
was decided. A trace is the decision path itself, captured as it happened and
complete enough to re-run.

Two things are provided here:

- **Recording.** A `RunTrace` is the serialisable form of a run: the goal, the
  principal, the limits in force, every step, and the *provenance* — which
  policy was applied and what the tools looked like at the time.
- **Replay.** The recorded decisions are fed back through the **real
  executor**, not a simulator. That distinction is the whole value: a
  simulator proves your simulator works, and replaying through the production
  loop proves the loop produces the recorded outcome.

Replay reports **divergence** rather than merely succeeding. If today's code
would not reproduce March's run, that is the finding, and it is far more
interesting than a confirmation.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from harness.planner import CallTool, Decision, Finish, PlannerState
from harness.policy import AuditPolicy, Principal, RiskTier, StandardAudit
from harness.run import (
    RunContext,
    RunLimits,
    RunOutcome,
    RunResult,
    StepKind,
    StepRecord,
)
from harness.tools import ToolRegistry

TRACE_FORMAT_VERSION = 1


class TraceError(Exception):
    """The trace cannot be read or replayed."""


def _digest(spec: dict[str, Any]) -> str:
    """A stable fingerprint of a tool's contract.

    Sorted keys so the digest depends on content rather than dict ordering.
    """
    return hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Provenance:
    """The world as it was when the run happened.

    Without this, replay compares a March run against an August system and
    calls any difference a divergence. With it, you can say *which* thing
    changed — the code, the policy, or the tool contract.
    """

    recorded_at: float
    trace_format: int = TRACE_FORMAT_VERSION
    authorization_policy: str = "unknown"
    audit_policy: str = "unknown"
    tool_digests: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RunTrace:
    """A run, in a form that outlives the process that produced it."""

    run_id: str
    goal: str
    principal_id: str
    tier: RiskTier
    limits: RunLimits
    steps: tuple[StepRecord, ...]
    outcome: RunOutcome
    cost_usd: float
    provenance: Provenance
    output: Any = None

    # ------------------------------------------------------------ serialise
    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_format": self.provenance.trace_format,
            "run_id": self.run_id,
            "goal": self.goal,
            "principal_id": self.principal_id,
            "tier": int(self.tier),
            "outcome": str(self.outcome),
            "cost_usd": self.cost_usd,
            "output": self.output,
            "limits": {
                "max_steps": self.limits.max_steps,
                "max_cost_usd": self.limits.max_cost_usd,
                "max_consecutive_failures": self.limits.max_consecutive_failures,
                "max_wall_clock_seconds": self.limits.max_wall_clock_seconds,
                "default_tool_timeout_seconds": self.limits.default_tool_timeout_seconds,
                "max_delegation_depth": self.limits.max_delegation_depth,
            },
            "provenance": {
                "recorded_at": self.provenance.recorded_at,
                "authorization_policy": self.provenance.authorization_policy,
                "audit_policy": self.provenance.audit_policy,
                "tool_digests": self.provenance.tool_digests,
            },
            "steps": [
                {
                    "index": s.index,
                    "kind": str(s.kind),
                    "summary": s.summary,
                    "tool_name": s.tool_name,
                    "arguments": s.arguments,
                    "result": s.result,
                    "error": s.error,
                    "cost_usd": s.cost_usd,
                }
                for s in self.steps
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunTrace:
        version = data.get("trace_format")
        if version != TRACE_FORMAT_VERSION:
            # Refusing beats guessing. A trace written by a future version may
            # mean something different by the same field name, and silently
            # misreading an audit record is worse than not reading it.
            raise TraceError(
                f"trace format {version!r} is not readable by version {TRACE_FORMAT_VERSION}"
            )
        p = data["provenance"]
        lim = data["limits"]
        return cls(
            run_id=data["run_id"],
            goal=data["goal"],
            principal_id=data["principal_id"],
            tier=RiskTier(data["tier"]),
            limits=RunLimits(
                max_steps=lim["max_steps"],
                max_cost_usd=lim["max_cost_usd"],
                max_consecutive_failures=lim["max_consecutive_failures"],
                # `.get` with the default, not `[...]`: a trace recorded before
                # time bounds existed must still load. A trace format that
                # cannot read its own history is not an audit record.
                max_wall_clock_seconds=lim.get("max_wall_clock_seconds", 300.0),
                default_tool_timeout_seconds=lim.get("default_tool_timeout_seconds", 30.0),
                max_delegation_depth=lim.get("max_delegation_depth", 3),
            ),
            steps=tuple(
                StepRecord(
                    index=s["index"],
                    kind=StepKind(s["kind"]),
                    summary=s["summary"],
                    tool_name=s["tool_name"],
                    arguments=s["arguments"],
                    result=s["result"],
                    error=s["error"],
                    cost_usd=s["cost_usd"],
                )
                for s in data["steps"]
            ),
            outcome=RunOutcome(data["outcome"]),
            cost_usd=data["cost_usd"],
            output=data.get("output"),
            provenance=Provenance(
                recorded_at=p["recorded_at"],
                trace_format=version,
                authorization_policy=p["authorization_policy"],
                audit_policy=p["audit_policy"],
                tool_digests=p["tool_digests"],
            ),
        )

    @classmethod
    def from_json(cls, raw: str) -> RunTrace:
        try:
            return cls.from_dict(json.loads(raw))
        except (json.JSONDecodeError, KeyError) as exc:
            raise TraceError(f"unreadable trace: {exc}") from exc


def record_trace(
    goal: str,
    ctx: RunContext,
    result: RunResult,
    registry: ToolRegistry | None = None,
    audit: AuditPolicy | None = None,
) -> RunTrace:
    """Capture a completed run.

    Arguments are recorded only where the audit policy permits. A tool whose
    arguments carry the sensitive part of a call can be excluded, at the cost
    of a trace that can be inspected but not fully replayed — which is the
    correct trade in some contexts and should be a deliberate one.
    """
    audit = audit or StandardAudit()

    steps = tuple(
        s
        if s.tool_name is None or audit.must_record_arguments(s.tool_name)
        else StepRecord(
            index=s.index,
            kind=s.kind,
            summary=s.summary,
            tool_name=s.tool_name,
            arguments=None,
            result=s.result,
            error=s.error,
            cost_usd=s.cost_usd,
            metadata={**s.metadata, "arguments_withheld": True},
        )
        for s in result.steps
    )

    digests: dict[str, str] = {}
    policy_name = "unknown"
    if registry is not None:
        policy_name = registry._authorization.name
        for name in registry.names:
            spec = registry.spec(name)
            digests[name] = _digest(
                {"description": spec.description, "parameters": spec.parameters}
            )

    return RunTrace(
        run_id=result.run_id,
        goal=goal,
        principal_id=ctx.principal.id,
        tier=ctx.tier,
        limits=ctx.limits,
        steps=steps,
        outcome=result.outcome,
        cost_usd=result.cost_usd,
        output=result.output,
        provenance=Provenance(
            recorded_at=time.time(),
            authorization_policy=policy_name,
            audit_policy=audit.name,
            tool_digests=digests,
        ),
    )


# =========================================================== replay
def decisions_from(trace: RunTrace) -> list[Decision]:
    """Reconstruct the planner's decisions from a recorded run.

    Built from the `PLAN` steps, which carry the intent, rather than from the
    `TOOL_CALL` steps, which carry the result. That distinction matters for a
    run stopped by a ceiling: its final decision was formed but never executed,
    so there is a `PLAN` with no `TOOL_CALL` after it. Reconstructing from
    results would silently drop that decision and the replay would finish
    normally where the original was cut off.
    """
    decisions: list[Decision] = []

    for step in trace.steps:
        if step.kind is StepKind.PLAN:
            if step.metadata.get("arguments_withheld"):
                raise TraceError(
                    f"step {step.index}: arguments were withheld at record time, "
                    "so this trace can be inspected but not replayed"
                )
            decisions.append(
                CallTool(
                    tool=step.tool_name or "",
                    arguments=step.arguments or {},
                    rationale=step.summary,
                )
            )
        elif step.kind is StepKind.FINISH:
            decisions.append(Finish(output=step.result, rationale=step.summary))

    return decisions


class ReplayPlanner:
    """Returns the recorded decisions, in order. Calls no model."""

    name = "replay"

    def __init__(self, trace: RunTrace) -> None:
        self._decisions = decisions_from(trace)
        self._index = 0

    async def decide(self, state: PlannerState) -> Decision:
        if self._index >= len(self._decisions):
            return Finish(output=None, rationale="trace exhausted")
        decision = self._decisions[self._index]
        self._index += 1
        return decision


class ReplayRegistry(ToolRegistry):
    """Returns recorded results instead of calling anything real.

    Subclasses the real registry so replay still passes through authorisation.
    That is deliberate: a replay that skipped policy could not tell you whether
    a run that was permitted in March would still be permitted today, which is
    one of the more useful things a replay can establish.
    """

    def __init__(self, trace: RunTrace, authorization: Any = None) -> None:
        super().__init__(authorization)
        self._recorded = [s for s in trace.steps if s.kind is StepKind.TOOL_CALL]
        self._cursor = 0

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        # Only the side effect is replaced. `check` is inherited untouched and
        # still runs from the executor, so replaying against today's registry
        # answers "would this run still be permitted" — which is usually the
        # question behind the request.
        if self._cursor >= len(self._recorded):
            raise TraceError(f"no recorded result for call {self._cursor} to {name!r}")
        step = self._recorded[self._cursor]
        self._cursor += 1
        if step.error is not None:
            raise RuntimeError(step.error)
        return step.result


@dataclass(frozen=True)
class Divergence:
    step_index: int
    field: str
    recorded: Any
    replayed: Any

    def __str__(self) -> str:
        return (
            f"step {self.step_index}: {self.field} "
            f"was {self.recorded!r}, replayed as {self.replayed!r}"
        )


@dataclass(frozen=True)
class ReplayReport:
    """What happened when the trace was re-run.

    `faithful` false is the interesting case, not the failure case. It means
    today's system would not reproduce that run, and the divergences say where
    it parts company.
    """

    run_id: str
    faithful: bool
    outcome_matches: bool
    divergences: tuple[Divergence, ...]
    replayed: RunResult

    def summary(self) -> str:
        if self.faithful:
            return f"{self.run_id}: faithful ({len(self.replayed.steps)} steps)"
        return f"{self.run_id}: DIVERGED — " + "; ".join(str(d) for d in self.divergences[:5])


def compare(trace: RunTrace, replayed: RunResult) -> tuple[Divergence, ...]:
    """Where the replayed run parts company with the recorded one."""
    out: list[Divergence] = []

    if trace.outcome is not replayed.outcome:
        out.append(Divergence(-1, "outcome", str(trace.outcome), str(replayed.outcome)))

    if len(trace.steps) != len(replayed.steps):
        out.append(Divergence(-1, "step_count", len(trace.steps), len(replayed.steps)))

    for recorded, actual in zip(trace.steps, replayed.steps, strict=False):
        if recorded.kind is not actual.kind:
            out.append(Divergence(recorded.index, "kind", str(recorded.kind), str(actual.kind)))
        if recorded.tool_name != actual.tool_name:
            out.append(
                Divergence(recorded.index, "tool_name", recorded.tool_name, actual.tool_name)
            )
        if recorded.arguments != actual.arguments:
            out.append(
                Divergence(recorded.index, "arguments", recorded.arguments, actual.arguments)
            )
        if (recorded.error is None) != (actual.error is None):
            out.append(Divergence(recorded.index, "error", recorded.error, actual.error))

    return tuple(out)


async def replay(
    trace: RunTrace,
    registry: ToolRegistry | None = None,
) -> ReplayReport:
    """Re-run a recorded trace through the real executor.

    Not a simulator. The same `run_agent` that served the original request
    drives the replay, with the planner and the tool results replaced by what
    was recorded. If the loop has changed since, the replay diverges — which
    is the finding.

    Pass a `registry` to replay against *today's* authorisation policy: the
    report then answers "would this run still be permitted", which is usually
    the question behind the request.
    """
    from harness.executor import run_agent

    replay_registry = ReplayRegistry(trace, authorization=getattr(registry, "_authorization", None))
    if registry is not None:
        for name in registry.names:
            spec = registry.spec(name)

            async def _unused(_arguments: dict[str, Any]) -> Any:  # pragma: no cover
                raise TraceError("replay must not reach a live tool")

            replay_registry.register(spec, _unused)
    else:
        seen: set[str] = set()
        for step in trace.steps:
            if step.kind is StepKind.TOOL_CALL and step.tool_name and step.tool_name not in seen:
                seen.add(step.tool_name)
                from harness.tools import ToolSpec

                async def _unused2(_arguments: dict[str, Any]) -> Any:  # pragma: no cover
                    raise TraceError("replay must not reach a live tool")

                replay_registry.register(
                    ToolSpec(
                        name=step.tool_name,
                        description="replayed",
                        parameters={"type": "object"},
                    ),
                    _unused2,
                )

    ctx = RunContext(
        principal=Principal(id=trace.principal_id),
        limits=trace.limits,
        tier=trace.tier,
        run_id=trace.run_id,
    )
    result = await run_agent(trace.goal, ReplayPlanner(trace), replay_registry, ctx)
    divergences = compare(trace, result)

    return ReplayReport(
        run_id=trace.run_id,
        faithful=not divergences,
        outcome_matches=trace.outcome is result.outcome,
        divergences=divergences,
        replayed=result,
    )
