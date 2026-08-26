# ADR-0007: Replay runs through the real executor, and reports divergence

- **Status:** Accepted
- **Date:** 2026-08-26

## Context
The question a regulated employer actually asks is not "is your agent
accurate". It is: *an agent did something in March; explain it.*

"The model is non-deterministic" does not survive that conversation. Neither
does a pile of log lines — logs record what was written down, not what was
decided.

## Decision
A `RunTrace` captures the decision path, and replay feeds it back through
**`run_agent` itself**, with the planner and the tool results substituted.

That is the load-bearing choice. A purpose-built replay simulator proves the
simulator works. Driving the production loop proves the loop reproduces the
recorded outcome — and if it does not, the loop has changed since, which is
the finding.

Four consequences:

- **Replay reports divergence rather than success.** `faithful=False` is the
  interesting case: today's system would not reproduce that run, and the
  divergences say where it parts company.
- **Policy still runs during replay.** `ReplayRegistry` substitutes the tool's
  *side effect*, not its authorisation. Replay against today's registry
  therefore answers "would this run still be permitted" — usually the question
  behind the request.
- **Provenance is recorded.** Which authorisation policy applied, and a digest
  of every tool's contract at the time. Without it, replay compares a March run
  against an August system and calls any difference a divergence; with it you
  can say *which* thing changed — the code, the policy, or the tool.
- **An unreadable trace is refused, not guessed.** A trace written by a future
  format version may mean something different by the same field name, and
  silently misreading an audit record is worse than not reading it.

## The bug this ADR exists because of
The `PLAN` step recorded only that thinking had happened, not what was
intended. For a run stopped by a ceiling that is the *last* thing the agent
decided — formed, then prevented — and it was being discarded. Replay
reconstructed decisions from `TOOL_CALL` steps, so the final prevented decision
vanished and the replay finished normally where the original was cut off.

The `PLAN` step now records `tool_name` and `arguments` before the ceilings are
checked, and decisions are reconstructed from plans rather than from results.
"The agent intended to call this and was stopped" is precisely what an auditor
asks about, and it is now in the record.

## A design change this forced
`ToolRegistry.check()` was extracted from `invoke()`. Replay needs the policy
without the side effect, and the first attempt called `invoke()` for the
authorisation — which also ran the tool. Separating them is better anyway: a
dry run and a pre-flight check need the same thing. `check` raises rather than
returning a verdict, because a caller that ignores a returned boolean is a bug
that looks like working code.

## Alternatives considered
- **A replay simulator.** Easier, and it verifies the simulator.
- **Replaying without policy.** Faster, and it cannot answer the question that
  prompts most replay requests.
- **Storing only inputs and outputs.** Compact, and it cannot show *why* — which
  is the entire point of the exercise.
- **Assuming a trace is readable if the fields are present.** Convenient until
  a field changes meaning between versions.

## Consequences
Traces are JSON and outlive the process, so results must be serialisable.
Where the audit policy withholds a tool's arguments, the trace is inspectable
but **not replayable**, and `decisions_from` raises rather than replaying with
empty arguments — a deliberate trade that fails loudly instead of quietly
producing a different run.
