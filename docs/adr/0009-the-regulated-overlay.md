# ADR-0009: One overlay module, not a fork

- **Status:** Accepted
- **Date:** 2026-08-26

## Context
ADR-0002 claimed that swapping regulatory context should be a module rather
than a fork. `src/harness/overlays.py` is that claim executed, and until it
existed the claim was untested.

## Decision
The overlay supplies different implementations of the same three protocols the
neutral core defines. Nothing in the core imports it. The core does not know it
exists.

| | Neutral | Regulated overlay |
|---|---|---|
| Authorisation | `RoleBasedAuthorization` — who is asking | `PurposeAuthorization` — **why** they are asking |
| Audit | `StandardAudit` | `RegulatedAudit` — longer retention, arguments withheld for sensitive tools |
| Oversight | none | `TierGate(FourEyesGate(...))` from `CONSEQUENTIAL` up |

The shape modelled is **permissible purpose**: an identity with the technical
ability to read a record may still have no lawful reason to. Two consequences:

- **A principal with no declared purpose is denied outright** — not narrowed,
  denied. An undeclared purpose is not a weak permission; it is an
  unanswerable question.
- **The purpose travels with each call**, so authority is established at the
  point of access rather than asserted once at the top of a workflow. That is
  the difference between an audit trail that can answer "under what authority
  was *this record* read" and one that cannot.

`regulated_overlay()` returns all three together, because they are not
independent: a tool sensitive enough to withhold its arguments usually needs a
purpose and a reviewer too, and configuring them in three places is how they
end up inconsistent.

## What the end-to-end test demonstrates
The same agent, tools, planner and executor, run four times:

1. **Neutral** — role holder proceeds, unsupervised, both tools succeed.
2. **Regulated, no purpose** — every call denied; nothing runs.
3. **Regulated, `account-review`** — reading permitted, scoring denied. The
   purpose is sufficient for one tool and not the other, which per-workflow
   authorisation could not express.
4. **Regulated, `credit-application`** — both permitted, and the consequential
   call is signed by a second person. The trace records `purpose-based` /
   `regulated`, and the sensitive tool's arguments are withheld.

Nothing changed but the module.

## Alternatives considered
- **A `regulated=True` flag in the core.** The core then knows about a
  regulatory regime, and the next regime adds a second flag.
- **A fork per jurisdiction.** They drift within a quarter and every fix is
  applied twice.
- **Configuration rather than code.** Works until a policy needs to read
  something — an entitlements service, a purpose registry — and then it needs
  to be code.
- **Shipping the overlay as the default.** Wrong for most deployments, and
  wrong defaults are trusted.

## Consequences
The retention figures in `RegulatedAudit` are illustrative and a real
deployment must set its own — that is a legal question, not an engineering one.
Withholding arguments makes a trace inspectable but **not replayable**, and
`decisions_from` raises rather than replaying with empty arguments, so the loss
is loud rather than silent. Anyone adapting this to another regime writes a
sibling module and leaves the core alone; if the core needs changing to
accommodate it, that is a defect in the core.
