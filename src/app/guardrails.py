"""Guardrail interfaces.

The template defines the contract and ships **no implementations**. What counts
as unacceptable input or output is a per-service policy question -- a credit
decisioning service and an internal documentation assistant do not share one --
and a template that guessed would be wrong for both.

Where guardrails attach matters, and it is easy to get wrong:

- **Request guardrails** run at the HTTP boundary, in middleware. Size limits,
  content-type checks, obvious abuse. Cheap, generic, and they belong here.
- **Model guardrails** run at the *model call* boundary, not the HTTP one:
  prompt-injection detection, PII redaction, output classification. Filtering an
  HTTP response body is the wrong abstraction -- by then you have lost which
  part came from a model, which came from retrieval, and which the service
  computed itself. Services import `Guardrail` and apply it around their own
  model calls.

A guardrail that blocks legitimate work gets switched off, and a switched-off
guardrail protects nothing. Test yours for false positives as carefully as for
false negatives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class GuardrailResult:
    """The outcome of one guardrail check.

    `allowed=False` blocks. `replacement` lets a guardrail redact rather than
    reject -- usually the better answer, since a redacted response is still a
    response.
    """

    allowed: bool
    reason: str | None = None
    replacement: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @classmethod
    def allow(cls) -> GuardrailResult:
        return cls(allowed=True)

    @classmethod
    def block(cls, reason: str, **metadata: str) -> GuardrailResult:
        return cls(allowed=False, reason=reason, metadata=metadata)

    @classmethod
    def redact(cls, replacement: str, reason: str, **metadata: str) -> GuardrailResult:
        return cls(allowed=True, reason=reason, replacement=replacement, metadata=metadata)


@runtime_checkable
class Guardrail(Protocol):
    """A named check over a piece of text."""

    name: str

    async def check(self, text: str) -> GuardrailResult: ...


class GuardrailChain:
    """Runs guardrails in order, stopping at the first block.

    Order is significant and is the caller's choice: put the cheap
    deterministic checks first so an obvious rejection never pays for a model
    call.
    """

    def __init__(self, *guardrails: Guardrail) -> None:
        self._guardrails: list[Guardrail] = list(guardrails)

    def add(self, guardrail: Guardrail) -> None:
        self._guardrails.append(guardrail)

    @property
    def names(self) -> list[str]:
        return [g.name for g in self._guardrails]

    async def run(self, text: str) -> tuple[str, list[GuardrailResult]]:
        """Return the (possibly redacted) text and every result produced.

        Raises `GuardrailBlockedError` on the first guardrail that blocks. Results
        are returned rather than discarded so the caller can record which
        guardrails fired -- guardrail hit rates are an evaluation signal, not
        just an operational one.
        """
        results: list[GuardrailResult] = []
        current = text
        for guardrail in self._guardrails:
            result = await guardrail.check(current)
            results.append(result)
            if not result.allowed:
                raise GuardrailBlockedError(guardrail.name, result)
            if result.replacement is not None:
                current = result.replacement
        return current, results


class GuardrailBlockedError(Exception):
    """Raised when a guardrail rejects the text."""

    def __init__(self, guardrail_name: str, result: GuardrailResult) -> None:
        self.guardrail_name = guardrail_name
        self.result = result
        super().__init__(f"blocked by {guardrail_name}: {result.reason}")
