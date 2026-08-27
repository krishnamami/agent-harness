# ADR-0017: A rate limit is a budget, not a permission

## Status

Accepted. Supersedes the rate-limiting behaviour shipped in v1.0.0.

## Context

`ToolRegistry` enforced a per-tool rate limit inside `check()`, using a list of
call timestamps held on the registered tool. Two problems, and the second is a
live defect rather than a design preference.

**It cannot be correct with more than one replica.** The counter lives in the
process. Three pods each enforcing "sixty a minute" permit a hundred and eighty,
and nothing reports the discrepancy — the limit appears to work, and the
downstream system is the one that finds out. `Memory` had already faced the same
question and answered it with a `MemoryStore` protocol plus an in-memory
reference implementation; the rate limiter shipped only the implementation. The
repository was applying its own pattern inconsistently.

**A dry run consumed real budget.** `check()` is documented as the way to ask
"would this call be permitted" without the side effect, and is used by pre-flight
checks and by replay. `ReplayRegistry` subclasses the real registry and overrides
only `call`, precisely so that replay still passes through authorisation. But
`check` was inherited untouched, so **replaying a trace from March consumed
today's quota**. Enough replays and an audit fails with `RateLimitExceededError`
— a failure that says nothing about the run being audited.

## Decision

Two changes.

**A `RateLimiter` protocol, with `InProcessRateLimiter` as the reference
implementation.** The protocol has a single method, `allow(tool_name,
limit_per_minute) -> bool`, which checks and consumes in one step. Splitting it
into check-then-record would open a race no shared backend could close. The
registry takes one by constructor injection and defaults to in-process, which is
right for one replica and wrong for two — naming the default in the constructor
is what makes that a choice rather than a surprise.

**Rate limiting moves from `check()` to `call()`.** `check()` is the permission
path and now consumes nothing. `call()` is where the tool actually runs, and a
budget should be spent by work rather than by asking whether work would be
permitted. `ReplayRegistry` overrides `call` entirely, so a replay costs nothing
without needing to know that rate limiting exists.

## Consequences

A call refused for rate rather than policy now fails *after* any approval it
required, instead of being filtered out before it. The executor deliberately
pre-flights authorisation so that a human is not asked to approve a call that
will be refused anyway; a rate-limited call now escapes that filter and wastes
an approval.

That is a real cost and it is the smaller one. Rate refusals are rare next to
policy denials, and the executor already catches `RateLimitExceededError` around
`call`, so the outcome is a recorded failed step rather than a crashed run. A
rate limit that a dry run can spend is a rate limit that does not mean what it
says, and correctness of replay is not tradeable against approval economy.

A reserve-then-release protocol would give both — pre-flight without consuming,
commit on execution. It is the right eventual answer and it is more machinery
than the problem currently justifies. Revisit when a shared backend exists and
approval volume makes the waste measurable.

## Rejected alternatives

**Make `check()` async and keep the limit there.** Correct-looking, and it does
not fix the replay defect at all — a replay calls `check`, so it would still
spend budget. It also breaks the public signature at v1.0.0 for no gain.

**Keep the protocol synchronous so `check()` can stay sync and still limit.** A
shared limiter needs I/O; a synchronous interface either blocks the event loop
or forces every implementation to do something clever. And it preserves the
replay defect.

**Leave it in-process and document the limitation.** Considered seriously: the
repository's own posture is that a stated boundary beats a leaky abstraction.
Rejected because the replay defect is not a limitation, it is a bug, and because
`Memory` already established that a store belongs behind a contract.
