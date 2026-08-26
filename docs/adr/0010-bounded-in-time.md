# ADR-0010: Runs are bounded in time, not only in steps and spend

- **Status:** Accepted
- **Date:** 2026-08-26

## Context

ADR-0003 claims a run is bounded by construction. It shipped with three
ceilings: steps, spend, and consecutive failures. Reviewing the loop against
the two use cases this harness is about to carry, the claim turned out to be
true of one kind of runaway and quietly false of another.

All three existing ceilings are checked *when the loop turns*. A run that is
blocked awaiting a downstream which never answers never turns the loop again.
It does not reach step two, so `max_steps` never fires. It records no cost, so
`max_cost_usd` never fires. It records no failure, so the give-up counter never
fires. The run simply waits, holding its resources, until something outside the
harness kills the process.

A hang is a more common production failure than a loop, and it was invisible to
every bound we had. The bound that reads best on a slide was missing the case
that happens most.

Nothing in `src/harness/` referenced a clock at all except the tool registry's
rate limiter.

## Decision

Two more bounds, both in `RunLimits`.

**`max_wall_clock_seconds`** (default 300) bounds the run. It is checked in
`before_step`, and it is checked **first** — before steps and before cost.
A run that has exhausted both time and steps reports `TIME_LIMIT`, because time
is what stopped it: it ran out first by definition, and an operator who saw
`STEP_LIMIT` would tune the wrong dial.

**`default_tool_timeout_seconds`** (default 30) bounds a single call, and a
`ToolSpec` may override it. Per-tool rather than global because one number is
always wrong for something: a row lookup taking thirty seconds is broken, and a
document extraction taking thirty seconds is normal. This is the same argument
the registry already makes about rate limits and risk tiers — the registry can
answer questions a list of functions cannot.

Every call is additionally **clamped to whatever the run has left**
(`RunContext.timeout_for`). Without the clamp the bound would hold for the call
and not for the run, which is the same as not holding.

The planner call is bounded too, by the run's remaining time. A planner is
usually a model call over a network and is the likeliest hang in the whole
loop; leaving it unbounded would have fixed the smaller half of the problem.

**A timeout is an observation, not an ending.** It is recorded as a failed
`TOOL_CALL` and returned to the planner, which may have another route. What
stops the agent waiting forever is the timeout; what stops it retrying a dead
downstream forever is the consecutive-failure ceiling from ADR-0005. This is
the same shape as a tool raising, and it is deliberately consistent with it.

`RunOutcome` gains `TIME_LIMIT`, counted separately from `FAILED` for the same
reason the other ceilings are: a run that hit a bound is the system working,
and conflating it with a crash means you cannot tune the bound.

## Consequences

- The claim in ADR-0003 is now true of hangs as well as loops.
- `RunLimits` gained two fields, so `RunTrace` gained two keys. Deserialisation
  reads them with defaults rather than by subscript: a trace recorded before
  this ADR must still load, because an audit record that cannot read its own
  history is not an audit record.
- Tests assert on the clock without waiting for it, by ageing the context's
  monotonic start. A test that sleeps its way to an assertion is one that
  eventually fails on a loaded CI runner for reasons unrelated to the code.
- The start time is `time.monotonic()`, not wall time. A run must not be
  extended or truncated by an NTP correction landing mid-flight.

## Rejected alternatives

**One timeout on the whole run, and none on individual calls.** Simpler, and it
does bound the run. But a single hung call then consumes the entire budget as
one call, and the trace says only that the run timed out — not which tool
stopped answering. The per-call bound is what makes a hang diagnosable rather
than merely survivable.

**Per-call timeouts only, with no run ceiling.** Twenty calls each comfortably
inside a thirty-second timeout still sum to ten minutes. Bounding the parts does
not bound the whole.

**Rely on the tool's own HTTP client timeout.** A tool is an arbitrary
coroutine, not necessarily an HTTP call, and the harness cannot assume every
tool author configured a client carefully. A bound that holds only when every
contributor remembers is policy-by-discipline, which is the thing this whole
repository argues against.

**A watchdog task that cancels a run from outside.** More machinery to reach
the same place, and it is monitoring rather than bounding — the distinction
ADR-0003 exists to make. `asyncio.wait_for` is the same cancellation with less
of it.

**Treating a timeout as terminal for the run.** Tempting, because a timeout
feels more serious than an ordinary error. Rejected: a slow downstream is
exactly the situation self-correction exists for, and a run that dies on one
slow call is brittle in the way ADR-0005 already rejected for failures. The
consecutive-failure ceiling bounds the retrying.

**Validating that `default_tool_timeout_seconds` fits inside
`max_wall_clock_seconds`.** This was written, and then removed. Because
`timeout_for` clamps every call unconditionally, the validation protected
nothing — it only rejected configurations that would have behaved correctly,
and it made the obvious `RunLimits(max_wall_clock_seconds=10)` raise on a
default the caller never chose. A check that cannot prevent a real failure but
can reject a reasonable config is a footgun with good intentions.
