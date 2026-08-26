# ADR-0001: Built on ai-golden-path@v1.0.0

- **Status:** Accepted
- **Date:** 2026-08-26

## Context
This service needs configuration, structured logging, correlation ids,
tracing, an error contract, health probes, a container and a CI gate before it
can do anything agent-shaped at all. None of that is specific to agents.

## Decision
Start from `ai-golden-path` at tag `v1.0.0`, pinned. The template supplies
twelve components; this repository adds `src/harness` and nothing else changes.

Scaffolding it required editing **four** lines across 39 inherited files — the
image name, the project name, the service-name default, and one string in the
evaluation stub. That number is the evidence the template is parameterised
rather than merely copyable.

The template's own ADRs (0001–0011) were deliberately **not** copied. They
record the template's decisions, not this service's, and duplicating them would
create two divergent copies of the same reasoning. They are referenced by
number where relevant.

## Alternatives considered
- **Start from scratch.** A week of plumbing already solved, and the result
  would drift from every other service in the estate.
- **Track the template's `main`.** Convenient until a template change breaks a
  service at an unrelated moment. A pinned tag makes upgrading a deliberate act
  with its own commit.
- **Vendor the template as a dependency.** Attractive, and wrong for a
  scaffold: services must be able to diverge from it. A golden path that cannot
  be edited locally becomes a framework, and teams route around frameworks.
- **Copy the ADRs too.** Produces two copies of the same decision that drift
  apart, and it is never clear which is authoritative.

## Consequences
Upgrading to a later template version is a deliberate change with a diff to
review, not something that happens on a Tuesday. If building the harness forces
a change to the template itself, that change belongs upstream in the template
rather than here — a fix applied only locally is how a golden path quietly
becomes a dirt road nobody takes.
