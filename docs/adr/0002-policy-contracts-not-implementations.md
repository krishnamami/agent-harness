# ADR-0002: The harness ships policy contracts, not policies

- **Status:** Accepted
- **Date:** 2026-08-26

## Context
Three things differ between one enterprise and the next, and none of them are
technical questions: what counts as authorised, what must be retained and for
how long, and how much oversight a run needs.

A harness that hard-codes one set of answers is usable only by the organisation
it was written for. A harness that ships plausible defaults is worse, because a
team assumes it is covered.

## Decision
Express all three as protocols with neutral, obviously-incomplete defaults:

| Protocol | Neutral default | What a regulated deployment substitutes |
|---|---|---|
| `AuthorizationPolicy` | `OpenAuthorization` (permits everything) and `RoleBasedAuthorization` | A policy that carries a *purpose* down to each tool call, not only an identity |
| `AuditPolicy` | `StandardAudit`, retention scaling with tier | Retention calibrated to the actual regulatory obligation |
| `RiskTier` | Four ordered tiers | The same four, mapped to that firm's control framework |

Three consequences of that shape are load-bearing:

- **Authorisation is consulted per tool call, never once per run.** A workflow
  that establishes authority at the top and then acts freely cannot answer
  "under what authority was this specific record read", which is the question
  that actually gets asked afterwards.
- **`Principal` carries an optional `purpose`.** Where authorisation depends on
  *why* rather than only *who* — permissible purpose under FCRA is the obvious
  case — the field is there without the neutral core knowing what it means.
- **A decision can carry obligations.** "Permitted, but redact this field" is a
  common real answer, and returning it with the decision keeps policy in one
  place rather than scattered across call sites.

This is the same move `ai-golden-path` makes with `Guardrail` and `Evaluator`,
one level up, for the same reason.

## Alternatives considered
- **Ship a production-grade default policy.** Wrong for most organisations, and
  wrong defaults are worse than absent ones because they are trusted.
- **A configuration file rather than protocols.** Works until a policy needs to
  read something — a purpose registry, an entitlements service — and then it
  needs to be code.
- **Fork per deployment.** The regulated version and the neutral version drift
  within a quarter, and every fix has to be applied twice.
- **Authorise once at the start of a run.** Cheaper, faster, and it produces an
  audit trail that cannot answer the only question anyone asks of it.

## Consequences
A deployment must supply real policies; the defaults are deliberately not
production-ready and `OpenAuthorization` is named so that nobody mistakes it.
Swapping regulatory context is a module, not a fork. The registry is the only
route to a tool, because a bypass is how audit trails develop holes.
