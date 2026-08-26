# ADR-0008: Oversight is tiered, and a refusal is terminal

- **Status:** Accepted
- **Date:** 2026-08-26

## Context
"Human in the loop" is easy to promise and easy to implement badly. A gate on
every call is the obvious design, and it is self-defeating: reviewers stop
reading within a fortnight, and a gate that is rubber-stamped protects nothing
while costing everything.

## Decision
A gate has a **threshold**. Below it, nothing happens and no step is recorded.
At or above it, a reviewer decides.

The tier of a call is `max(run tier, tool tier)` — a routine run calling a
critical tool is a critical call, and the reverse holds too.

Four properties are load-bearing:

- **A refusal is terminal.** `RunOutcome.NOT_APPROVED`, distinct from `DENIED`.
  An authorisation denial is an observation the planner may route around; a
  human saying no is not an obstacle to work around. Feeding a refusal back
  would produce an agent that rephrases until someone says yes.
- **`ApprovalDecision.gated` is explicit, not inferred.** "Approved" and
  "nobody looked" are different facts, and only one belongs in an audit trail.
  Recording an approval for every ungated call would pad routine runs with
  entries asserting an oversight nobody performed. `AutoApprove` returns
  `not_gated`, and it is named so that nobody reads a configuration and
  believes a control exists.
- **Approvals are recorded as steps.** Who approved what, at what tier. An
  approval that leaves no trace is indistinguishable from no approval,
  eighteen months later.
- **An approval step moves neither the failure streak nor the plan count.** It
  is neither an action nor planning.

`FourEyesGate` removes the requesting principal from the eligible reviewers,
because self-approval is the exact failure a four-eyes control exists to
prevent and is easy to reintroduce when the approver comes from the same
session as the request.

## The bug this ADR exists because of
The first implementation asked the gate **before** checking authorisation. The
end-to-end demo showed `supervisor-1` approving a call that policy then refused
for lack of permissible purpose — a human spending attention on something that
was never going to happen.

Beyond the waste, it is corrosive: reviewers who learn that their approvals do
not determine outcomes stop treating them as decisions.

Authorisation is now checked first, and the gate is consulted only for calls
that would actually proceed. This forced `ToolRegistry` to split `invoke` into
`check` and `call`, so the executor can authorise, gate, and then execute
without paying the rate limit twice.

## Alternatives considered
- **Gate every call.** Reviewers stop reading. The control decays into a
  formality that is worse than none, because it is documented as a control.
- **Gate by tool only.** Ignores that the same tool is riskier in some runs.
- **Treat a refusal as an observation.** Produces an agent that negotiates
  with its reviewer.
- **Infer "was this gated" from the approver name.** Works until someone names
  a reviewer badly, and encodes a control decision in a string.

## Consequences
Ungated runs are byte-identical to runs before gates existed — the gate is
opt-in and defaults to `AutoApprove`, which records nothing. Setting the
threshold is a governance decision, not an engineering one, and `RecordingGate`
exists partly so that the set of calls reaching a human can be reviewed as
evidence about whether the threshold sits in the right place.
