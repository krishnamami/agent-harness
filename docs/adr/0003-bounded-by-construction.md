# ADR-0003: Runs are bounded by construction, not by monitoring

- **Status:** Accepted
- **Date:** 2026-08-26

## Context
"What stops an agent running away?" is the first question anyone asks about an
agent platform, and "we monitor it" is not an answer. Monitoring detects; it
does not stop. By the time an alert fires the money is spent and the actions
are taken.

## Decision
Every run carries `RunLimits` and cannot exceed them:

- **A step ceiling**, so a loop terminates.
- **A cost ceiling**, checked *before* a step rather than after — a run that
  discovers it is over budget has already spent it.
- **A consecutive-failure ceiling**, which is the explicit give-up condition.

Both ceilings default to bounded values (25 steps, $1.00) rather than to
unlimited, because a default of unlimited is the setting nobody revisits and
the first time it matters is the invoice.

A success resets the failure streak, so a run that recovers is not killed by
failures it already survived.

## Alternatives considered
- **Monitor and alert.** Detects after the fact. Useful in addition, useless
  instead.
- **A wall-clock timeout only.** Does not bound cost — a run can spend a great
  deal in sixty seconds — and does not distinguish "slow" from "looping".
- **Unlimited by default, limits opt-in.** The limits then exist only on the
  runs whose author already thought about it, which are the runs least likely
  to need them.
- **Unbounded self-correction.** Without a give-up condition, "self-correcting"
  means "fails slowly and expensively".

## Consequences
A bounded run is not a failed run: `RunOutcome` distinguishes `STEP_LIMIT`,
`COST_LIMIT` and `GAVE_UP` from `FAILED`, because hitting a ceiling is the
system working and should be counted separately when tuning the ceilings.
Every step is recorded as it happens, which is what makes the replay in a later
session possible — a trace reassembled afterwards from logs is never quite
complete enough to reconstruct a decision.
