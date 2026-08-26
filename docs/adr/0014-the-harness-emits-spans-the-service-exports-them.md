# ADR-0014: The harness emits spans; the service exports them

- **Status:** Accepted
- **Date:** 2026-08-26

## Context

ADR-0001 said the golden path supplies tracing, and the harness then never used
it. `src/harness/` contained no reference to OpenTelemetry at all. A run
produced a `RunTrace` — an audit record, which is a different thing — and was
otherwise invisible: nothing in Tempo, Jaeger or any collector, no way to see
which tool was slow, and, once delegation landed, no way to see the shape of an
agent tree at all.

Adding spans is easy. The question worth deciding is what goes *in* them,
because a span and a trace go to different places, and only one of them is
governed by `AuditPolicy`.

## Decision

**The harness imports the OpenTelemetry API and nothing else.** `get_tracer` is
a no-op until something installs a provider. A service that wants traces
installs one — `app.telemetry` does, with an OTLP exporter and parent-based
sampling — and a service that does not pays close to nothing. A library that
configured its own exporter would be making a deployment decision on behalf of
every service that imports it. `opentelemetry-api` is declared explicitly
rather than leaned on transitively through the SDK, because that is the
dependency the library actually has.

**Five span kinds**: `agent.run`, `agent.plan`, `agent.tool`, `agent.approval`,
`agent.delegate`.

**The span tree is the agent tree.** A delegation's span wraps the child's
`agent.run`, so a coordinator with four workers looks like a coordinator with
four workers. Parallel calls become sibling `agent.tool` spans under the same
run — an overlapping timeline is what parallelism looks like in a backend, and
a serialised one is what a regression looks like.

**Arguments and results never go on a span.** This is the load-bearing rule.
`AuditPolicy` governs what a *trace* retains, and a regulated deployment uses it
to withhold the arguments of sensitive tools. A span goes somewhere else: an
observability backend, with its own retention, its own access control, and
usually a much wider audience than the audit store. Putting arguments on spans
would route around the audit policy through a side door — the same shape of
mistake batching nearly made in ADR-0013. Spans carry names, tiers, counts,
durations, outcomes and cost. Identities too, because an approval nobody can
attribute is not an approval. Never payloads.

**A bounded stop is not an error.** `STEP_LIMIT`, `COST_LIMIT`, `TIME_LIMIT`,
`DEPTH_LIMIT`, `GAVE_UP` and `NOT_APPROVED` all leave the span status `OK` with
`harness.outcome` set. Only `FAILED` sets `ERROR`. Marking ceilings as errors
would make the error rate on every dashboard a measure of how often the bounds
did their job, and would bury the one case that genuinely is an error.

**Error reasons are low cardinality.** `harness.error.kind` is one of `denied`,
`timeout`, `unavailable`, `failed`, `refused`, `not_approved` — a closed set you
can group by. The detail goes in the status message, which backends do not
index. A stringified exception in an attribute is unbounded cardinality and will
ruin an attribute index inside a week.

**Naming follows the GenAI semantic conventions where one exists**
(`gen_ai.operation.name`, `gen_ai.tool.name`) and uses a `harness.` namespace
where one does not.

## Consequences

- Log lines emitted inside a run now carry a real `trace_id` and `span_id`,
  where they previously showed `-`. The two-identifier support loop the golden
  path built — correlation id to log line, log line to trace — now actually
  reaches the agent.
- `run_agent` split into a span wrapper and `_loop`, so the run span closes on
  every exit from the loop rather than on the happy path only. `_decide` was
  extracted for the same reason: the plan span has to close on the two paths
  that end the run, not just the one that returns a decision.
- The harness now has a hard dependency on the OTel API. It is a small, stable,
  vendor-neutral package, and the alternative — an internal abstraction over
  tracing so the dependency stays optional — is a layer nobody would thank us
  for.

## Rejected alternatives

**Configure an exporter inside the harness.** Convenient, and it decides for
every service that imports the library where its telemetry goes.

**Put arguments on spans for debuggability.** The single most tempting one, and
the reason this ADR exists. Debugging an agent from a backend without seeing the
arguments is genuinely harder. But the trace already holds them, governed, with
retention set by risk tier and redaction set by policy — and duplicating them
into a store with different access control means the audit policy protects
nothing. If a deployment wants arguments in its backend, it can export the
trace deliberately, which is a decision someone makes rather than one the
harness makes for them.

**Mark every non-completed outcome as a span error.** Simpler branchless code,
and it makes "error rate" meaningless on any dashboard built over it.

**Invent `gen_ai.*` attributes for our own concepts.** `gen_ai.risk_tier` would
look conformant to a backend that then indexes nothing, because no such
convention exists. An honest `harness.` namespace is better than a plausible
lie.

**One span for the whole run.** Cheapest, and it cannot answer which tool hung,
whether the batch actually ran in parallel, or what the tree looked like —
which is most of why we wanted spans.
