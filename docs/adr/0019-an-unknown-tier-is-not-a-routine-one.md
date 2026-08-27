# ADR-0019: An unknown tier is not a routine one

## Status

Accepted.

## Context

Until now every tool was registered by hand by the deployment that ran the
agent, so `ToolSpec.tier` was written by whoever wrote the tool. `tool-registry`
changes that: the tool surface arrives over MCP from a service, and the tier
arrives with it, in `_meta.risk_tier`.

That is fine when the field is there and the harness recognises the value. The
interesting case is when it is not:

- a server that does not publish risk metadata at all
- `_meta` present but empty
- a tier name from a vocabulary this harness version does not know (`"severe"`)
- a non-string value

`ToolSpec.tier` defaults to `ROUTINE`, and `ROUTINE` is what `TierGate` waves
through. So the natural implementation — read the field, fall back to the
dataclass default — means **a tool whose risk the harness could not read
executes with no review at all.** The failure is silent and it is precisely
inverted: the tools most likely to have unreadable metadata are the ones from an
unfamiliar or newer server, which is not a good reason to trust them more.

## Decision

An unreadable tier resolves to `CONSEQUENTIAL`, not `ROUTINE`.

```python
declared = meta.get("risk_tier")
if not isinstance(declared, str):
    return fallback              # default CONSEQUENTIAL
return TIERS.get(declared.lower(), fallback)
```

The fallback is a parameter, so a deployment that knows its estate can raise it
to `CRITICAL` or lower it deliberately. What it cannot do is get `ROUTINE` by
accident.

Over-gating is a nuisance: someone reviews a call that did not need reviewing,
notices, and fixes the metadata. Under-gating is an incident. The asymmetry is
not close, and it is the whole argument.

**A declared `routine` is honoured.** The fallback is not a floor. A harness
that treated every remote tool as consequential regardless of what the server
said would make tiering non-proportionate, and a gate that fires on everything
gets routed around — which leaves less oversight than before there was a policy
(ADR-0008).

**The served schema is registered unedited.** Rewriting it locally would mean the
harness validates against a contract the server does not hold, and every
disagreement surfaces as an unexplained remote refusal rather than as a local
`ToolArgumentError` the planner can act on.

**Refusal and failure stay distinct.** The server returns `isError: true` for
both "you may not do this" and "it ran and broke". The client separates them: a
refusal becomes `ToolDeniedError`, which the executor already feeds back to the
planner as an observation; an action that ran and failed becomes
`RemoteToolError`, carrying the `action_record_id`. The first question in an
incident is whether anything changed out there, and flattening both into one
exception destroys the answer.

**No transport ships here.** `Transport` is a protocol with one method. stdio,
HTTP and a test double all satisfy it — the same posture as `MemoryStore`,
`RateLimiter` and `ApprovalGate`. A harness that also owned a socket would be two
products (ADR-0002).

## Consequences

Local `rate_limit_per_minute` is now explicitly the harness's *own* budget,
passed at load time rather than read from the server. The server's limit is the
one that binds; the local one exists so a runaway loop is stopped in-process
rather than by a remote refusal a model then has to interpret. Naming that in
the signature keeps someone from later reading the local limit as the control.

A server that publishes no risk metadata will have every one of its tools gated.
That is loud, which is the point — it should be fixed at the server, and it will
be noticed within one run.

This is the third defect in this repository of the same family: a default that
looks harmless because the code path that exposes it is not the one the tests
take. ADR-0017 (a dry run spending real budget), ADR-0018 (a schema nobody
checked), and now a tier nobody could read. The pattern worth naming: **when a
value can be absent, ask what the absent case permits, not what it looks like.**
