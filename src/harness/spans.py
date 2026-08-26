"""Spans, and what deliberately does not go in them.

The harness *emits* spans. It does not configure where they go, and it does not
import the application. `get_tracer` from the OpenTelemetry **API** is a no-op
until something installs a provider, so a service that wants traces installs one
-- `app.telemetry` does, with an OTLP exporter and parent-based sampling -- and
a service that does not pays close to nothing. A library that configured its own
exporter would be making a deployment decision on behalf of every service that
imports it.

**Arguments and results never go on a span.**

This is the rule that matters. `AuditPolicy` decides what a *trace* may retain,
and a regulated deployment uses it to withhold the arguments of sensitive tools
-- at the cost of a trace that can be inspected but not replayed, which is the
correct trade in some contexts. A span goes somewhere else entirely: an
observability backend, with its own retention, its own access control, and
usually a much wider audience than the audit store.

Putting arguments on spans would route around the audit policy through a side
door, exactly the way batching nearly did in ADR-0013. So spans carry names,
tiers, counts, durations, outcomes and cost. Identities too -- which principal
ran, which approver decided -- because an approval nobody can attribute is not
an approval. Never payloads.

Attribute naming follows the OpenTelemetry GenAI semantic conventions where a
convention actually exists (`gen_ai.operation.name`, `gen_ai.tool.name`), and
uses a `harness.` namespace where one does not. Inventing plausible-looking
`gen_ai.*` keys would be worse than having our own: it would look conformant to
a backend that then indexed nothing.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from harness.run import RunContext, RunOutcome, RunResult

tracer = trace.get_tracer("harness")

RUN = "agent.run"
PLAN = "agent.plan"
TOOL = "agent.tool"
APPROVAL = "agent.approval"
DELEGATE = "agent.delegate"


def describe_run(span: Span, ctx: RunContext) -> None:
    """Put the bounds a run is starting under onto its span.

    The limits are recorded at the start rather than the end because the most
    useful question about a run that stopped early is what it was allowed to do,
    and a span that only says `step_limit` cannot answer it.
    """
    span.set_attribute("gen_ai.operation.name", "invoke_agent")
    span.set_attribute("harness.run_id", ctx.run_id)
    span.set_attribute("harness.principal.id", ctx.principal.id)
    span.set_attribute("harness.tier", str(ctx.tier))
    span.set_attribute("harness.depth", ctx.depth)
    span.set_attribute("harness.limits.max_steps", ctx.limits.max_steps)
    span.set_attribute("harness.limits.max_cost_usd", ctx.limits.max_cost_usd)
    span.set_attribute("harness.limits.max_wall_clock_seconds", ctx.limits.max_wall_clock_seconds)
    span.set_attribute("harness.limits.max_parallel_calls", ctx.limits.max_parallel_calls)


def record_outcome(span: Span, result: RunResult) -> None:
    """Close a run span with what happened.

    A run stopped by a ceiling is **not** an error. Marking it one would make
    the error rate on every dashboard a measure of how often the bounds did
    their job, and would bury the case that genuinely is an error: a run that
    crashed. `harness.outcome` is the attribute to alert on if you want to
    watch ceilings; the span status is reserved for failure.
    """
    span.set_attribute("harness.outcome", str(result.outcome))
    span.set_attribute("harness.steps", len(result.steps))
    span.set_attribute("harness.cost_usd", round(result.cost_usd, 6))

    if result.outcome is RunOutcome.FAILED:
        span.set_status(Status(StatusCode.ERROR, result.error or "run failed"))
    else:
        span.set_status(Status(StatusCode.OK))


def record_error(span: Span, kind: str, detail: str) -> None:
    """Mark a span failed, with a low-cardinality reason.

    `kind` is one of a small closed set -- `denied`, `timeout`, `unavailable`,
    `failed` -- so it can be grouped by. The detail goes in the status message,
    which backends do not index, because a stringified exception is unbounded
    cardinality and will ruin an attribute index given a week.
    """
    span.set_attribute("harness.error.kind", kind)
    span.set_status(Status(StatusCode.ERROR, detail))
